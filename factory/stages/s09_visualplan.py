"""s09 — визуальный план (shot list) по реальному таймлайну озвучки.

Каждый сегмент получает слот на шкале; слот режется на планы длиной по Visual
Rhythm. Маркер сценария определяет тип плана; к планам добавляются зум-панчи на
акцентах, счётчики на акцентных числах, цветокоррекция по акту и затемнение-вход
на границе смысловых блоков. Границы планов выровнены по кадрам.
"""
from __future__ import annotations

from ..core.context import Ctx
from ..core.schema import Script, plain_text
from ..engagement.visual_rhythm import grade_for, parse_number, shot_length, split_durations

OUTPUTS = ["shots.json"]
KIND = {"STOCK": "stock", "IMAGE": "image", "PARALLAX": "parallax", "TEXT": "text", "COUNTER": "counter",
        "CHART": "chart"}
GRAPHIC_MAX_S = 8.0


def segment_slots(timeline: dict) -> dict[str, tuple[float, float]]:
    """Слоты сегментов покрывают всю шкалу без дыр: паузы делятся пополам между соседями."""
    segs = timeline["segments"]
    slots = {}
    for i, s in enumerate(segs):
        a = 0.0 if i == 0 else (segs[i - 1]["end"] + s["start"]) / 2
        b = timeline["duration"] if i == len(segs) - 1 else (s["end"] + segs[i + 1]["start"]) / 2
        slots[s["id"]] = (a, b)
    return slots


def build_shots(sc: Script, timeline: dict, cfg, width: int, height: int) -> list[dict]:
    fps = int(cfg.get("render.fps", 30))
    vcfg = cfg.section("visual")
    shot_cfg = vcfg.get("shot_s", {})
    min_len = float(shot_cfg.get("min", 1.4))
    beats = {b.block_id: b for b in sc.beats}
    segs = {s.id: s for s in sc.segments}
    tl_segs = {s["id"]: s for s in timeline["segments"]}
    slots = segment_slots(timeline)
    shots: list[dict] = []
    fallback_visual = None
    for seg_id, (a, b) in slots.items():
        seg = segs[seg_id]
        beat = beats[seg.block_id]
        vis = seg.visual.model_dump(exclude_none=True)
        if vis["marker"] in ("STOCK", "IMAGE", "PARALLAX"):
            fallback_visual = vis
        words = tl_segs[seg_id]["words"]
        emph_times = [w["start"] for w in words if w.get("emph")]
        counters = []
        if vcfg.get("counters_on_emph_numbers", True) and vis["marker"] not in ("COUNTER", "CHART"):
            for w in words:
                num = parse_number(w["w"]) if w.get("emph") else None
                if num and num[0] >= 3:
                    counters.append({"t": w["start"], "value": num[0], "suffix": num[1], "dur": 2.2})
        kind = KIND[vis["marker"]]
        pieces: list[tuple[float, float, dict, str]] = []
        if kind in ("text", "counter", "chart"):
            g_end = min(b, a + GRAPHIC_MAX_S)
            pieces.append((a, g_end, vis, kind))
            if b - g_end >= min_len:        # длинный слот: после графики — b-roll
                fb = fallback_visual or {"marker": "IMAGE", "prompt": f"documentary b-roll, {sc.topic}"}
                pieces.append((g_end, b, fb, KIND[fb["marker"]]))
            elif b > g_end:
                pieces[-1] = (a, b, vis, kind)
        else:
            pieces.append((a, b, vis, kind))
        for (pa, pb, pvis, pkind) in pieces:
            if pkind in ("text", "counter", "chart"):
                durs = [pb - pa]
            else:
                durs = split_durations(pb - pa, shot_length(beat.act, beat.tension, shot_cfg), min_len)
            t = pa
            for k, d in enumerate(durs):
                sa, sb = t, t + d
                t = sb
                punches = sorted({round(x - sa, 3) for x in emph_times if sa + 0.1 <= x < sb - 0.35})
                dedup: list[float] = []
                for p in punches:
                    if not dedup or p - dedup[-1] > 1.2:
                        dedup.append(p)
                shot = {
                    "id": f"sh{len(shots) + 1:03d}", "block_id": seg.block_id, "seg_id": seg_id, "start": sa, "end": sb,
                    "kind": pkind, "visual": pvis, "variant": len(shots) + k,
                    "register": beat.visual_register, "grade": grade_for(beat.act, beat.tension, beat.visual_register),
                    "punches": dedup[:3] if pkind in ("stock", "image") else [],
                    "counters": [{**c, "t": round(c["t"] - sa, 3)} for c in counters if sa <= c["t"] < sb - 0.8],
                    "fade_in": float(vcfg.get("fade_in_on_block_s", 0.15))
                    if shots and shots[-1]["block_id"] != seg.block_id else 0.0,
                    "text": plain_text(pvis.get("content", "")) if pkind == "text" else "",
                    "emph_words": [w["w"] for w in words if w.get("emph")],
                }
                shots.append(shot)
    # выравнивание по кадрам: сумма кадров = длительность ролика
    for s in shots:
        s["f0"] = round(s["start"] * fps)
        s["f1"] = round(s["end"] * fps)
    shots[-1]["f1"] = round(timeline["duration"] * fps)
    shots = [s for s in shots if s["f1"] > s["f0"]]
    for i in range(1, len(shots)):
        shots[i]["f0"] = shots[i - 1]["f1"]
    for s in shots:
        s["frames"] = s["f1"] - s["f0"]
    return shots


def run(ctx: Ctx) -> dict:
    sc = Script.model_validate(ctx.job.read_json("script.json"))
    timeline = ctx.job.read_json("timeline.json")
    w, h = int(ctx.cfg.get("render.width", 1920)), int(ctx.cfg.get("render.height", 1080))
    shots = build_shots(sc, timeline, ctx.cfg, w, h)
    ctx.job.write_json("shots.json", {"width": w, "height": h, "fps": int(ctx.cfg.get("render.fps", 30)),
                                      "shots": shots})
    kinds: dict[str, int] = {}
    for s in shots:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    lens = [s["end"] - s["start"] for s in shots]
    return {"shots": len(shots), "kinds": kinds, "avg_shot_s": round(sum(lens) / len(lens), 2),
            "punches": sum(len(s["punches"]) for s in shots), "counters": sum(len(s["counters"]) for s in shots)}
