"""Восемь проверок QA-гейта. Каждая возвращает CheckResult с указанием, с какой стадии
чинить и что именно сказать модели/стадии при ремонте."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core import media
from ..core.context import Ctx
from ..core.db import now
from ..core.schema import DIRECT_SPEECH_ROLES, Script, plain_text
from ..engagement.beatsheet_rules import act_sequence, register_sequence, validate_beats
from ..engagement.format_rotation import recent_formats, template_similarity
from ..engagement.voice_strategy import check_invariants
from ..render.fonts import font

log = logging.getLogger("factory.qa")


@dataclass
class CheckResult:
    name: str
    passed: bool
    summary: str
    problems: list[str] = field(default_factory=list)
    repair_from: str | None = None
    feedback: dict[str, list[str]] = field(default_factory=dict)   # стадия → что исправить
    notes: list[str] = field(default_factory=list)


def _overlap(a0: float, a1: float, windows: list[tuple[float, float]], tol: float = 0.3) -> bool:
    return any(a0 >= w0 - tol and a1 <= w1 + tol for w0, w1 in windows)


# 1 ---------------------------------------------------------------- техника
def technical(ctx: Ctx) -> CheckResult:
    q = ctx.cfg.section("qa")
    master = ctx.job.p("out", "master.mp4")
    probs: list[str] = []
    repair: list[str] = []
    fb: dict[str, list[str]] = {}
    info = media.probe(master)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not v or v.get("codec_name") != "h264" or v.get("pix_fmt") != "yuv420p":
        probs.append(f"видео не h264/yuv420p: {v and v.get('codec_name')}/{v and v.get('pix_fmt')}")
        repair.append("render")
    if not a or a.get("codec_name") != "aac":
        probs.append("нет AAC-дорожки")
        repair.append("render")
    dur = float(info["format"]["duration"])
    err = media.run_stderr(["ffmpeg", "-i", str(master), "-vf", f"blackdetect=d={q.get('black_max_s', 1.5)}:pix_th=0.08",
                            "-an", "-f", "null", "-"])
    blacks = re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)
    for s, e in blacks:
        probs.append(f"чёрный кадр {float(s):.1f}–{float(e):.1f} с")
        repair.append("assets")
    tl = ctx.job.read_json("timeline.json")
    planned = [(s["start"], s["end"]) for s in tl.get("planned_silences", [])]
    err = media.run_stderr(["ffmpeg", "-i", str(master), "-af",
                            f"silencedetect=n={q.get('silence_db', -50)}dB:d={q.get('silence_max_s', 2.0)}",
                            "-vn", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)] + [dur]
    for s, e in zip(starts, ends, strict=False):
        if not _overlap(s, e, planned):
            probs.append(f"тишина {s:.1f}–{e:.1f} с ({e - s:.1f} с) вне запланированных пауз")
            repair.append("humanize")
    from ..stages.s11_sound import measure_lufs  # noqa: PLC0415
    lufs, _ = measure_lufs(master)
    target, tol = float(q.get("lufs_target", -14)), float(q.get("lufs_tolerance", 1.0))
    if abs(lufs - target) > tol:
        probs.append(f"громкость {lufs:.1f} LUFS, нужно {target}±{tol}")
        repair.append("sound")
    sheet_target = float(ctx.job.read_json("beatsheet.json")["target_duration_s"])
    dtol = float(q.get("duration_tolerance", 0.25))
    if abs(dur - sheet_target) > dtol * sheet_target:
        what = "длиннее" if dur > sheet_target else "короче"
        probs.append(f"ролик {dur:.0f} с — на {abs(dur - sheet_target) / sheet_target:.0%} {what} цели {sheet_target:.0f} с")
        repair.append("script")
        fb["script"] = [f"озвучка получилась {dur:.0f} с при цели {sheet_target:.0f} с — "
                        f"{'сократи' if dur > sheet_target else 'расширь'} текст примерно на "
                        f"{abs(dur - sheet_target) / dur:.0%}, сохранив структуру блоков"]
    shorts = json.loads(ctx.job.p("shorts", "shorts.json").read_text()) if ctx.job.exists("shorts/shorts.json") else []
    for sh in shorts:
        sp = media.probe(ctx.job.p(sh["file"]))
        sv = next(s for s in sp["streams"] if s["codec_type"] == "video")
        if float(sp["format"]["duration"]) > 60.5 or sv["height"] <= sv["width"]:
            probs.append(f"шортс {sh['file']} не 9:16 или длиннее 60 с")
            repair.append("clips")
    order = _stage_order()
    return CheckResult("technical", not probs, f"{dur:.1f} с, {lufs:.1f} LUFS, чёрных {len(blacks)}, тишина вне плана "
                       f"{sum(1 for p in probs if p.startswith('тишина'))}", probs,
                       min(repair, key=order.index) if repair else None, fb)


def _stage_order() -> list[str]:
    from ..core.pipeline import STAGE_NAMES  # noqa: PLC0415
    return STAGE_NAMES


# 2 ---------------------------------------------------------------- синхрон и safe-zone
def sync(ctx: Ctx) -> CheckResult:
    tol = float(ctx.cfg.get("qa.sync_tolerance_s", 0.15))
    tl = ctx.job.read_json("timeline.json")
    probs = []
    for seg in tl["segments"]:
        prev = -1.0
        for w in seg["words"]:
            if w["start"] < seg["start"] - tol or w["end"] > seg["end"] + tol:
                probs.append(f"{seg['id']}: слово «{w['w']}» ({w['start']:.2f}) вне своего сегмента "
                             f"[{seg['start']:.2f}; {seg['end']:.2f}]")
                break
            if w["start"] + tol < prev:
                probs.append(f"{seg['id']}: тайминги слов идут назад")
                break
            prev = w["start"]
    for ass in sorted(ctx.job.p("subs").glob("*.ass")):
        text = ass.read_text(encoding="utf-8")
        W = int(re.search(r"PlayResX: (\d+)", text).group(1))
        H = int(re.search(r"PlayResY: (\d+)", text).group(1))
        style = re.search(r"Style: Default,([^,]+),(\d+),.*,(\d+),1$", text, re.M)
        size, margin_v = int(style.group(2)), int(style.group(3))
        if H > W and margin_v < H * float(ctx.cfg.get("shorts.safe_bottom_frac", 0.22)) - 1:
            probs.append(f"{ass.name}: субтитры в зоне интерфейса площадки (MarginV {margin_v})")
        for line in re.findall(r"^Dialogue: [^,]*,[^,]*,[^,]*,[^,]*,[^,]*,\d+,\d+,\d+,,(.*)$", text, re.M)[:400]:
            fs = re.match(r"\{\\fs(\d+)\}", line)
            f = font(int(fs.group(1)) if fs else size)
            clean = re.sub(r"\{[^}]*\}", "", line)
            if f.getlength(clean) * 1.12 > W * 0.94:
                probs.append(f"{ass.name}: строка не влезает в кадр: «{clean[:40]}…»")
                break
    return CheckResult("sync", not probs, f"слов {sum(len(s['words']) for s in tl['segments'])}, "
                       f"выравнивание: {tl.get('alignment')}", probs, "align" if probs else None)


# 3 ---------------------------------------------------------------- факты
NUMBER_RE = re.compile(r"\d")


def facts(ctx: Ctx, sc: Script) -> CheckResult:
    src_ids = {s.id for s in sc.sources}
    probs, fb, notes = [], [], []
    for seg in sc.segments:
        txt = plain_text(seg.text)
        needs = bool(NUMBER_RE.search(txt)) or seg.role == "quote"
        if needs and not [s for s in seg.source_ids if s in src_ids]:
            probs.append(f"{seg.id}: утверждение с числом/цитата без источника: «{txt[:70]}»")
            fb.append(f"сегмент {seg.id}: добавь source_ids из списка источников или убери непроверяемое число/цитату")
    if any(s.id == "r00" for s in sc.sources):
        notes.append("часть фактов опирается на знания модели (r00) — Википедия не нашла материалов")
    judged = 0
    if ctx.cfg.get("qa.factcheck_llm_judge", True) and not ctx.cfg.offline and not probs:
        research = ctx.job.read_json("research.json")
        claims = [{"id": s.id, "text": plain_text(s.text), "source_ids": s.source_ids} for s in sc.segments
                  if s.source_ids]
        if claims:
            user = ctx.prompt("judge", segments=claims,
                              facts=[{"source": f["source_id"], "claim": f["claim"]} for f in research["facts"]])
            try:
                res = ctx.llm.json("judge", "Ты — строгий фактчекер. Отвечай JSON.", user, max_tokens=3000,
                                   temperature=0.1)
                for it in res.get("issues", []):
                    if it.get("verdict") in ("unsupported", "distorted"):
                        probs.append(f"{it.get('segment_id')}: {it.get('verdict')} — {it.get('problem', '')}"[:200])
                        fb.append(f"сегмент {it.get('segment_id')}: {it.get('problem', '')}. {it.get('fix', '')}"[:300])
                judged = len(claims)
            except Exception as e:  # noqa: BLE001 — судья недоступен: детерминированная часть уже прошла
                notes.append(f"LLM-судья недоступен: {e}")
    return CheckResult("facts", not probs, f"сегментов с источниками {sum(1 for s in sc.segments if s.source_ids)}, "
                       f"проверено судьёй {judged}", probs, "script" if probs else None,
                       {"script": fb} if fb else {}, notes)


# 4 ---------------------------------------------------------------- безопасность
def safety(ctx: Ctx, sc: Script) -> CheckResult:
    path = ctx.cfg.path("qa.stop_words_file", "config/stop_words.txt")
    words = [w.strip().lower() for w in path.read_text(encoding="utf-8").splitlines()
             if w.strip() and not w.startswith("#")] if path.exists() else []
    probs, notes = [], []
    text = " ".join(plain_text(s.text) for s in sc.segments).lower()
    meta = ctx.job.read_json("metadata.json") if ctx.job.exists("metadata.json") else {}
    meta_text = json.dumps(meta, ensure_ascii=False).lower()
    for w in words:
        if re.search(rf"\b{re.escape(w)}\b", text):
            probs.append(f"стоп-слово в сценарии: {w}")
        if re.search(rf"\b{re.escape(w)}\b", meta_text):
            probs.append(f"стоп-слово в метаданных: {w}")
    nsfw_cfg = ctx.cfg.section("qa.nsfw")
    try:
        from nudenet import NudeDetector  # noqa: PLC0415
        det = NudeDetector()
        master = ctx.job.p("out", "master.mp4")
        dur = media.duration(master)
        step = float(nsfw_cfg.get("sample_every_s", 10))
        t = 1.0
        while t < dur:
            fp = ctx.job.p("scenes", f"nsfw_{int(t)}.jpg")
            media.run(["ffmpeg", "-ss", f"{t:.1f}", "-i", str(master), "-frames:v", "1", str(fp)])
            bad = [d for d in det.detect(str(fp)) if "EXPOSED" in d.get("class", "") and d.get("score", 0) > 0.6]
            if bad:
                probs.append(f"NSFW на {t:.0f} с: {bad[0]['class']}")
            fp.unlink(missing_ok=True)
            t += step
    except ImportError:
        if nsfw_cfg.get("required"):
            probs.append("NSFW-классификатор обязателен, но не установлен (pip install nudenet)")
        else:
            notes.append("NSFW-классификатор не установлен; ассеты — модерируемый сток и генерация с фильтрами")
    repair = None
    if probs:
        repair = "assets" if any(p.startswith("NSFW") for p in probs) else "script"
    return CheckResult("safety", not probs, f"стоп-слов {len(words)}", probs, repair,
                       {"script": [f"убери: {p}" for p in probs if "стоп-слово" in p]} if probs else {}, notes)


# 5 ---------------------------------------------------------------- дедупликация
def shingles(text: str, n: int = 5) -> set[str]:
    t = re.sub(r"[^\w ]", "", plain_text(text).lower())
    t = re.sub(r"\s+", " ", t)
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def dedup(ctx: Ctx, sc: Script) -> CheckResult:
    mine = shingles(" ".join(s.text for s in sc.segments))
    thr = float(ctx.cfg.get("qa.dedup_threshold", 0.55))
    worst = (0.0, None)
    for r in ctx.db.all("SELECT job_id, shingles FROM scripts_index WHERE job_id != ?", (sc.job_id,)):
        other = set(json.loads(r["shingles"]))
        if not other:
            continue
        sim = len(mine & other) / max(1, len(mine | other))
        if sim > worst[0]:
            worst = (sim, r["job_id"])
    ctx.db.execute("INSERT INTO scripts_index(job_id, topic, shingles, created_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(job_id) DO UPDATE SET shingles=excluded.shingles, topic=excluded.topic",
                   (sc.job_id, sc.topic, json.dumps(sorted(mine)[:20000]), now()))
    ok = worst[0] < thr
    probs = [] if ok else [f"сценарий на {worst[0]:.0%} совпадает с выпуском {worst[1]}"]
    return CheckResult("dedup", ok, f"макс. сходство {worst[0]:.0%}" + (f" с {worst[1]}" if worst[1] else ""), probs,
                       None if ok else "beatsheet",
                       {} if ok else {"beatsheet": [f"прошлый выпуск {worst[1]} слишком похож — возьми другой угол и "
                                                    f"другую последовательность актов"]})


# 6 ---------------------------------------------------------------- антишаблон
def antitemplate(ctx: Ctx, sc: Script) -> CheckResult:
    thr = float(ctx.cfg.get("qa.antitemplate_register_similarity", 0.8))
    gap = int(ctx.cfg.get("formats.min_gap", 3))
    sims = template_similarity(ctx.db, sc.job_id, act_sequence(sc.beats), register_sequence(sc.beats))
    probs = []
    recent = [f for f in recent_formats(ctx.db, gap + 1)][1:]      # первый — этот же джоб
    same_format = sc.content_type in recent
    for s in sims:
        if s["acts"] >= 0.95 and s["registers"] >= thr:
            probs.append(f"структура совпадает с {s['job_id']} (акты {s['acts']:.0%}, регистры {s['registers']:.0%})")
    if same_format and ctx.meta().get("format_given") is None:
        probs.append(f"формат {sc.content_type} повторяется чаще, чем раз в {gap} выпуска")
    return CheckResult("antitemplate", not probs,
                       f"макс. сходство структуры {max((s['acts'] for s in sims), default=0):.0%}", probs,
                       "beatsheet" if probs else None,
                       {"beatsheet": ["структура и визуальный ритм повторяют прошлые выпуски — поменяй порядок актов, "
                                      "регистры и форму кульминации"]} if probs else {})


# 7 ---------------------------------------------------------------- драматургия по реальным длительностям
def dramaturgy(ctx: Ctx, sc: Script) -> CheckResult:
    tl = ctx.job.read_json("timeline.json")
    durations = {b["block_id"]: b["end"] - b["start"] for b in tl["blocks"]}
    # План проверяется строго на стадии beatsheet; здесь — что реальная озвучка не ушла от него далеко.
    # Длительности синтеза всегда отклоняются от плана на 10–20 %, поэтому пороги с допуском.
    tol = float(ctx.cfg.get("qa.dramaturgy_tolerance", 0.15))
    rules = dict(ctx.cfg.section("beatsheet"))
    for k in ("max_beat_s", "cold_open_max_s", "open_loop_max_start_s"):
        rules[k] = float(rules.get(k, {"max_beat_s": 40, "cold_open_max_s": 10, "open_loop_max_start_s": 45}[k])) * (1 + tol)
    rules["open_loop_close_min_frac"] = float(rules.get("open_loop_close_min_frac", 0.66)) * (1 - tol)
    probs = validate_beats(sc.beats, rules, None, durations)
    return CheckResult("dramaturgy", not probs, f"cold open {durations[sc.beats[0].block_id]:.1f} с, блоков "
                       f"{len(sc.beats)}", probs, "script" if probs else None,
                       {"script": [f"по реальной озвучке: {p}" for p in probs]} if probs else {})


# 8 ---------------------------------------------------------------- голоса
def voices(ctx: Ctx, sc: Script) -> CheckResult:
    tl = ctx.job.read_json("timeline.json")
    actual = {s["id"]: s["end"] - s["start"] for s in tl["segments"]}
    probs = check_invariants(sc, ctx.cfg, actual)
    n_voices = len({s["voice_id"] for s in tl["segments"]})
    direct = sum(1 for s in sc.segments if s.role in DIRECT_SPEECH_ROLES)
    return CheckResult("voices", not probs, f"стратегия {sc.voice_plan.strategy if sc.voice_plan else '?'}, "
                       f"голосов {n_voices}, реплик/цитат {direct}", probs, "voiceplan" if probs else None,
                       {"voiceplan": probs} if probs else {})


def run_all(ctx: Ctx) -> list[CheckResult]:
    sc = Script.model_validate(ctx.job.read_json("script.json"))
    results = []
    for fn in (technical, sync):
        results.append(fn(ctx))
    for fn in (facts, safety, dedup, antitemplate, dramaturgy, voices):
        results.append(fn(ctx, sc))
    return results


def write_feedback(path: Path, results: list[CheckResult]) -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for r in results:
        if r.passed:
            continue
        for stage, items in r.feedback.items():
            data.setdefault(stage, [])
            for it in items:
                if it not in data[stage]:
                    data[stage].append(it)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
