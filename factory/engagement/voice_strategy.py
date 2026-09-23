"""Voice Strategy: схема голосов выбирается по типу и структуре контента.

Роли сегментов размечает LLM (main_narrator, secondary_narrator, character, quote),
но сколько голосов брать и кому какой — решает детерминированный код, а не модель.

Инварианты (проверяются кодом, а не промтом):
  1. Нарраторская роль (main/secondary) меняется только на границе смыслового блока.
     Прямая речь (quote, character) может звучать внутри блока — реплика персонажа
     или цитата сама по себе является смысловой единицей.
  2. Непрерывный отрезок второго нарратора короче min_role_duration_s схлопывается в main.
  3. Смен нарратора не больше max_switches_per_minute в любом 60-секундном окне.
  4. Первый и последний нарраторские сегменты — main_narrator; последний сегмент ролика — main.
  5. secondary_narrator не ведёт подряд больше secondary_max_consecutive_blocks блоков.
Плюс: реплика короче min_direct_speech_s читается нарратором (никаких смен голоса
ради одного слова); один и тот же speaker всегда звучит одним голосом.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass

from ..core.config import Config
from ..core.db import DB, now
from ..core.schema import DIRECT_SPEECH_ROLES, MARKUP_TOKEN_RE, Script, Segment, VoicePlan, plain_text
from ..providers.tts.voice_library import VoiceLibrary, VoiceSpec


def estimate_seconds(text: str, cps: float) -> float:
    pauses = sum(int(m.group(3)) for m in MARKUP_TOKEN_RE.finditer(text) if m.group(2) == "pause") / 1000
    return len(plain_text(text)) / cps + pauses + 0.25


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


@dataclass
class Rules:
    min_role_s: float
    max_switches_per_min: int
    secondary_max_blocks: int
    min_direct_s: float
    max_quotes_per_min: int
    max_characters: int

    @classmethod
    def from_cfg(cls, cfg: Config) -> Rules:
        return cls(
            min_role_s=float(cfg.get("voice.min_role_duration_s", 12)),
            max_switches_per_min=int(cfg.get("voice.max_switches_per_minute", 2)),
            secondary_max_blocks=int(cfg.get("voice.secondary_max_consecutive_blocks", 2)),
            min_direct_s=float(cfg.get("voice.min_direct_speech_s", 1.5)),
            max_quotes_per_min=int(cfg.get("voice.max_quotes_per_minute", 2)),
            max_characters=int(cfg.get("voice.max_characters", 3)),
        )


# ============================================================ выбор стратегии

def choose_strategy(script: Script, cfg: Config) -> tuple[str, list[str]]:
    reasons: list[str] = []
    ct = script.content_type
    rule = cfg.get(f"voice.strategies.{ct}", "single")
    roles = Counter(s.role for s in script.segments)
    n_blocks = len(script.beats)
    speakers_char = {s.speaker for s in script.segments if s.role == "character"}
    if isinstance(rule, dict):          # explainer: простой или сложный
        if roles["quote"]:
            strategy = rule.get("with_quotes", "single_plus_quote")
            reasons.append(f"{ct}: есть цитаты ({roles['quote']}) → {strategy}")
        elif n_blocks > int(rule.get("simple_max_blocks", 6)) and roles["secondary_narrator"]:
            strategy = "dual_narrator"
            reasons.append(f"{ct}: сложный ({n_blocks} блоков) и сценарий размечает второго нарратора → "
                           f"dual_narrator")
        else:
            strategy = rule.get("simple", "single")
            reasons.append(f"{ct}: простой ({n_blocks} блоков, без цитат) → один голос")
    else:
        strategy = rule
        reasons.append(f"{ct} → {strategy} по таблице voice.strategies")
    if strategy == "cast" and not speakers_char:
        strategy = "single_plus_quote" if roles["quote"] else "single"
        reasons.append(f"стратегия cast, но в сценарии нет реплик персонажей → понижена до {strategy}")
    return strategy, reasons


def auto_secondary_blocks(script: Script) -> set[str]:
    """Если LLM не разметил второго нарратора в двухголосой стратегии — назначаем по актам."""
    beats = script.beats
    ct = script.content_type
    inner = [b for b in beats[1:-1]]
    if ct == "myth_vs_fact":
        return {b.block_id for b in inner if b.act == "myth"}
    if ct == "listicle":
        items = [b for b in inner if b.act == "list_item"]
        return {b.block_id for i, b in enumerate(items) if i % 2 == 1}
    if ct == "comparison":
        rising = [b for b in inner if b.act in ("rising", "twist")]
        return {b.block_id for i, b in enumerate(rising) if i % 2 == 1}
    # investigation и прочие: контекст и вторая линия расследования
    picks = [b for b in inner if b.act in ("context",)]
    rising = [b for b in inner if b.act == "rising"]
    picks += rising[1:2]
    return {b.block_id for b in picks}


# ============================================================ нормализация ролей

def _durations(segments: list[Segment], cfg: Config, actual: dict[str, float] | None) -> dict[str, float]:
    cps = float(cfg.get("voice.est_chars_per_second", 14.5))
    return {s.id: (actual or {}).get(s.id, estimate_seconds(s.text, cps)) for s in segments}


def _block_narrator_runs(segments: list[Segment], dur: dict[str, float]) -> list[dict]:
    """Непрерывные отрезки одной нарраторской роли. Прямая речь внутри отрезок не рвёт."""
    runs: list[dict] = []
    t = 0.0
    for s in segments:
        if s.role not in DIRECT_SPEECH_ROLES:
            if runs and runs[-1]["role"] == s.role:
                runs[-1]["end"] = t + dur[s.id]
                if runs[-1]["blocks"][-1] != s.block_id:
                    runs[-1]["blocks"].append(s.block_id)
                runs[-1]["segs"].append(s.id)
            else:
                runs.append({"role": s.role, "start": t, "end": t + dur[s.id], "blocks": [s.block_id],
                             "segs": [s.id]})
        elif runs:
            runs[-1]["end"] = t + dur[s.id]
        t += dur[s.id]
    return runs


def normalize_roles(script: Script, strategy: str, cfg: Config,
                    actual: dict[str, float] | None = None) -> tuple[list[Segment], list[str]]:
    rules = Rules.from_cfg(cfg)
    allowed = set(cfg.get(f"voice.roles_by_strategy.{strategy}", ["main_narrator"]))
    segs = [s.model_copy(deep=True) for s in script.segments]
    dur = _durations(segs, cfg, actual)
    log: list[str] = []

    # 0. роли, которых нет в стратегии
    for s in segs:
        if s.role in allowed:
            continue
        old = s.role
        if s.role == "character" and "quote" in allowed:
            s.role = "quote"
        else:
            s.role = "main_narrator"
            if old in DIRECT_SPEECH_ROLES:
                s.speaker = None
        log.append(f"{s.id}: роль {old} не входит в стратегию {strategy} → {s.role}")

    # 0b. двухголосая стратегия без разметки второго нарратора
    if "secondary_narrator" in allowed and not any(s.role == "secondary_narrator" for s in segs):
        picks = auto_secondary_blocks(script)
        for s in segs:
            if s.block_id in picks and s.role == "main_narrator":
                s.role = "secondary_narrator"
        if picks:
            log.append(f"второй нарратор не размечен — назначен по актам на блоки {sorted(picks)}")

    # 1. прямая речь короче порога читается нарратором
    for s in segs:
        if s.role in DIRECT_SPEECH_ROLES and dur[s.id] < rules.min_direct_s:
            log.append(f"{s.id}: реплика {dur[s.id]:.1f} с < {rules.min_direct_s} с — смена голоса ради неё "
                       f"звучит механически, читает нарратор")
            s.role, s.speaker = "main_narrator", None

    # 1b. нарраторская роль постоянна внутри блока (по доминирующей длительности)
    by_block: dict[str, Counter] = {}
    for s in segs:
        if s.role not in DIRECT_SPEECH_ROLES:
            by_block.setdefault(s.block_id, Counter())[s.role] += dur[s.id]
    block_role = {b: c.most_common(1)[0][0] for b, c in by_block.items()}
    for s in segs:
        if s.role not in DIRECT_SPEECH_ROLES and s.role != block_role[s.block_id]:
            log.append(f"{s.id}: смена нарратора внутри блока {s.block_id} запрещена → {block_role[s.block_id]}")
            s.role = block_role[s.block_id]

    order = [b.block_id for b in script.beats]
    narrated = [b for b in order if b in block_role]

    def set_block(bid: str, role: str, why: str) -> None:
        if block_role.get(bid) == role:
            return
        block_role[bid] = role
        for s in segs:
            if s.block_id == bid and s.role not in DIRECT_SPEECH_ROLES:
                s.role = role
        log.append(f"блок {bid} → {role}: {why}")

    # 4. рамка канала: первый и последний нарраторские блоки — main
    if narrated:
        set_block(narrated[0], "main_narrator", "ролик открывает главный голос")
        set_block(narrated[-1], "main_narrator", "ролик закрывает главный голос")

    # 5. второй нарратор не дольше N блоков подряд
    streak = 0
    for bid in narrated:
        if block_role[bid] == "secondary_narrator":
            streak += 1
            if streak > rules.secondary_max_blocks:
                set_block(bid, "main_narrator", f"второй нарратор уже ведёт {rules.secondary_max_blocks} блока подряд")
                streak = 0
        else:
            streak = 0

    # 2 и 3. короткие отрезки второго нарратора и частота смен — итеративно
    for _ in range(50):
        runs = _block_narrator_runs(segs, dur)
        short = [r for r in runs if r["role"] == "secondary_narrator" and r["end"] - r["start"] < rules.min_role_s]
        if short:
            r = min(short, key=lambda r: r["end"] - r["start"])
            for bid in r["blocks"]:
                set_block(bid, "main_narrator",
                          f"отрезок второго голоса {r['end'] - r['start']:.1f} с < {rules.min_role_s:.0f} с")
            continue
        switches = [runs[i]["start"] for i in range(1, len(runs))]
        viol = None
        for i, t0 in enumerate(switches):
            inside = [t for t in switches[i:] if t < t0 + 60]
            if len(inside) > rules.max_switches_per_min:
                viol = t0
                break
        if viol is None:
            break
        cands = [r for r in runs if r["role"] == "secondary_narrator" and r["end"] > viol and r["start"] < viol + 60]
        r = min(cands, key=lambda r: r["end"] - r["start"])
        for bid in r["blocks"]:
            set_block(bid, "main_narrator",
                      f"больше {rules.max_switches_per_min} смен голоса в минуте с {viol:.0f} с")

    # последний сегмент ролика — главный голос
    if segs and segs[-1].role != "main_narrator":
        log.append(f"{segs[-1].id}: ролик должен заканчиваться главным голосом")
        segs[-1].role, segs[-1].speaker = "main_narrator", None

    # плотность цитат для не-cast стратегий
    if strategy != "cast":
        quotes = [s for s in segs if s.role == "quote"]
        total = sum(dur.values())
        limit = max(1, int(rules.max_quotes_per_min * total / 60))
        if len(quotes) > limit:
            for s in sorted(quotes, key=lambda s: dur[s.id])[: len(quotes) - limit]:
                log.append(f"{s.id}: цитат больше {rules.max_quotes_per_min}/мин — короткую читает нарратор")
                s.role, s.speaker = block_role.get(s.block_id, "main_narrator"), None

    # ограничение числа персонажей
    chars = [sp for sp, _ in Counter(s.speaker for s in segs if s.role == "character").most_common()]
    for extra in chars[rules.max_characters:]:
        for s in segs:
            if s.speaker == extra:
                s.role, s.speaker = block_role.get(s.block_id, "main_narrator"), None
        log.append(f"персонаж {extra!r} лишний (максимум {rules.max_characters}) — его реплики пересказывает "
                   f"нарратор")
    return segs, log


# ============================================================ назначение голосов

def _score(v: VoiceSpec, *, gender: str, want_native: bool | None, archive: bool, penalized: set[str],
           taken_bases: set[str], seed: str) -> float:
    sc = 0.0
    if gender != "unknown":
        sc += 3.0 if v.gender == gender else -3.0
    if want_native is not None:
        sc += 2.0 if v.native == want_native else -1.5
    if archive:
        sc += 2.5 if v.is_archive else -1.0
    elif v.is_archive:
        sc -= 2.0
    if v.id in penalized:
        sc -= 1.5
    if v.voice in taken_bases:
        sc -= 2.0          # тот же физический голос у другого спикера — хуже различим
    return sc + (_h(seed + v.id) % 1000) / 10000.0


def _is_historic(era: str | None) -> bool:
    if not era:
        return False
    m = re.search(r"(1[0-9]{3})", era)
    return bool(m and int(m.group(1)) < 1980)


def assign_voices(script: Script, segments: list[Segment], strategy: str, library: VoiceLibrary, cfg: Config,
                  db: DB | None) -> VoicePlan:
    decisions: list[str] = []
    series = cfg.get("channel.series", "main")
    main_id = cfg.get("channel.main_voice") or library.defaults.get("main_narrator")
    main = library.get(main_id)
    assignments = {"main_narrator": main.id}
    used_ids = {main.id}
    used_bases = {main.voice}
    decisions.append(f"main_narrator = {main.id} ({main.voice}) — голос канала, постоянный")

    roles_present = {s.role for s in segments}
    if "secondary_narrator" in roles_present:
        pool = [library.get(v) for v in library.defaults.get("secondary_narrator_pool", [])]
        pool = [v for v in pool if v.id != main.id] or [v for v in library.for_role("secondary_narrator")
                                                        if v.id != main.id]
        if strategy == "dual_opposing":
            opp = [v for v in pool if v.gender != main.gender] or pool
            sec = opp[_h(series) % len(opp)]
            decisions.append(f"secondary_narrator = {sec.id} — оппонент контрастного тембра к главному голосу")
        else:
            sec = pool[_h(series) % len(pool)]
            decisions.append(f"secondary_narrator = {sec.id} — стабилен в серии {series!r}, ротируется между "
                             f"сериями")
        assignments["secondary_narrator"] = sec.id
        used_ids.add(sec.id)
        used_bases.add(sec.voice)

    penalized: set[str] = set()
    if db is not None:
        last = db.one("SELECT job_id FROM voice_history WHERE job_id != ? ORDER BY created_at DESC LIMIT 1",
                      (script.job_id,))
        if last:
            penalized = {r["voice_id"] for r in db.all(
                "SELECT voice_id FROM voice_history WHERE job_id=? AND role='character'", (last["job_id"],))}

    speakers = {sp.name: sp for sp in script.speakers}
    speaker_voices: dict[str, str] = {}
    order: list[tuple[str, str]] = []
    for s in segments:
        if s.role in DIRECT_SPEECH_ROLES and s.speaker and s.speaker not in speaker_voices:
            order.append((s.speaker, s.role))
            speaker_voices[s.speaker] = ""
    for name, role in order:
        sp = speakers.get(name)
        gender = sp.gender if sp else "unknown"
        origin = sp.origin if sp else "native"
        archive = role == "quote" and _is_historic(sp.era if sp else None)
        want_native = origin == "native"
        cands = [v for v in library.for_role(role) if v.id not in used_ids]
        if not cands:
            cands = [v for v in library.for_role(role) if v.id not in (main.id, assignments.get("secondary_narrator"))]
            decisions.append(f"голоса для роли {role} кончились — {name!r} получит повтор")
        best = max(cands, key=lambda v: _score(v, gender=gender, want_native=want_native, archive=archive,
                                                penalized=penalized, taken_bases=used_bases,
                                                seed=script.job_id + name))
        speaker_voices[name] = best.id
        used_ids.add(best.id)
        used_bases.add(best.voice)
        why = []
        if gender != "unknown":
            why.append(gender)
        why.append("иностранец → голос с акцентом" if not want_native else "родной голос")
        if archive:
            why.append(f"историческая цитата ({sp.era}) → архивная обработка")
        if best.id in penalized:
            why.append("повтор из прошлого выпуска (других не осталось)")
        decisions.append(f"{role} {name!r} = {best.id} ({', '.join(why)})")

    if "quote" in roles_present:
        assignments["quote"] = library.defaults.get("quote_default", "quote_plain_m")
    return VoicePlan(strategy=strategy, assignments=assignments, speaker_voices=speaker_voices,
                     decisions=decisions)


def voice_for_segment(seg: Segment, plan: VoicePlan) -> str:
    if seg.role in DIRECT_SPEECH_ROLES and seg.speaker and seg.speaker in plan.speaker_voices:
        return plan.speaker_voices[seg.speaker]
    return plan.assignments.get(seg.role) or plan.assignments["main_narrator"]


def record_voice_history(db: DB, script: Script, plan: VoicePlan, series: str) -> None:
    db.execute("DELETE FROM voice_history WHERE job_id=?", (script.job_id,))
    for role, vid in plan.assignments.items():
        db.execute("INSERT INTO voice_history(job_id, role, speaker, voice_id, series, created_at) "
                   "VALUES(?,?,?,?,?,?)", (script.job_id, role, None, vid, series, now()))
    kinds = {s.speaker: s.role for s in script.segments if s.speaker}
    for spk, vid in plan.speaker_voices.items():
        db.execute("INSERT INTO voice_history(job_id, role, speaker, voice_id, series, created_at) "
                   "VALUES(?,?,?,?,?,?)", (script.job_id, kinds.get(spk, "character"), spk, vid, series, now()))


# ============================================================ проверка (QA №8) и объяснение

def check_invariants(script: Script, cfg: Config, actual: dict[str, float] | None = None) -> list[str]:
    rules = Rules.from_cfg(cfg)
    segs = script.segments
    dur = _durations(segs, cfg, actual)
    p: list[str] = []
    for bid in {s.block_id for s in segs}:
        roles = {s.role for s in segs if s.block_id == bid and s.role not in DIRECT_SPEECH_ROLES}
        if len(roles) > 1:
            p.append(f"в блоке {bid} нарратор меняется внутри блока: {sorted(roles)}")
    runs = _block_narrator_runs(segs, dur)
    for r in runs:
        if r["role"] == "secondary_narrator" and r["end"] - r["start"] < rules.min_role_s - 0.5:
            p.append(f"второй голос звучит {r['end'] - r['start']:.1f} с (< {rules.min_role_s:.0f} с) в блоках "
                     f"{r['blocks']}")
    switches = [runs[i]["start"] for i in range(1, len(runs))]
    for i, t0 in enumerate(switches):
        n = sum(1 for t in switches[i:] if t < t0 + 60)
        if n > rules.max_switches_per_min:
            p.append(f"{n} смен нарратора за минуту начиная с {t0:.0f} с (максимум {rules.max_switches_per_min})")
            break
    if runs and (runs[0]["role"] != "main_narrator" or runs[-1]["role"] != "main_narrator"):
        p.append("ролик должен открываться и закрываться главным голосом")
    if segs and segs[-1].role != "main_narrator":
        p.append("последний сегмент ролика — не главный голос")
    streak, prev_block = 0, None
    for s in segs:
        if s.role in DIRECT_SPEECH_ROLES or s.block_id == prev_block:
            continue
        prev_block = s.block_id
        streak = streak + 1 if s.role == "secondary_narrator" else 0
        if streak > rules.secondary_max_blocks:
            p.append(f"второй нарратор ведёт больше {rules.secondary_max_blocks} блоков подряд (до {s.block_id})")
            break
    for s in segs:
        if s.role in DIRECT_SPEECH_ROLES and dur[s.id] < rules.min_direct_s - 0.2:
            p.append(f"{s.id}: реплика {dur[s.id]:.1f} с — слишком короткая для отдельного голоса")
    if script.voice_plan:
        spk: dict[str, set[str]] = {}
        for s in segs:
            if s.speaker and s.role in DIRECT_SPEECH_ROLES:
                spk.setdefault(s.speaker, set()).add(voice_for_segment(s, script.voice_plan))
        for name, vs in spk.items():
            if len(vs) > 1:
                p.append(f"speaker {name!r} звучит разными голосами: {sorted(vs)}")
    return p


def explain_switches(script: Script, cfg: Config, actual: dict[str, float] | None = None) -> list[str]:
    dur = _durations(script.segments, cfg, actual)
    plan = script.voice_plan
    lines, t, prev = [], 0.0, None
    beats = {b.block_id: b for b in script.beats}
    for s in script.segments:
        vid = voice_for_segment(s, plan) if plan else s.role
        cur = (s.role, vid)
        if prev and cur != prev:
            b = beats[s.block_id]
            if s.role in DIRECT_SPEECH_ROLES:
                why = f"прямая речь: {s.speaker}"
            elif prev[0] in DIRECT_SPEECH_ROLES:
                why = "возврат к нарратору после реплики"
            else:
                why = f"граница смыслового блока → {s.block_id} ({b.act}: {b.intent[:50]})"
            lines.append(f"{int(t // 60):02d}:{t % 60:04.1f}  {prev[0]}/{prev[1]} → {s.role}/{vid}  — {why}")
        prev = cur
        t += dur[s.id]
    return lines
