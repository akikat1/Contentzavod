"""Валидатор драматургии.

Проверяется дважды: на beat sheet (плановые длительности) и в QA на готовом
таймлайне (реальные длительности после синтеза речи). Возвращает список
человекочитаемых проблем — они же уходят обратно в LLM как текст ошибки.
"""
from __future__ import annotations

from collections.abc import Sequence

from ..core.schema import Beat


def beat_spans(beats: Sequence[Beat], durations: dict[str, float] | None = None) -> list[tuple[float, float]]:
    spans, t = [], 0.0
    for b in beats:
        d = (durations or {}).get(b.block_id, b.target_duration_s)
        spans.append((t, t + d))
        t += d
    return spans


def peak_indices(values: Sequence[float], prominence: float) -> list[int]:
    """Локальные максимумы с заданной выраженностью (topographic prominence)."""
    n = len(values)
    peaks = []
    for i, v in enumerate(values):
        left_ok = i == 0 or v > values[i - 1]
        right_ok = i == n - 1 or v >= values[i + 1]
        if not (left_ok and right_ok):
            continue
        bases = []
        if i > 0:
            j, lo = i - 1, v
            while j >= 0 and values[j] <= v:
                lo = min(lo, values[j])
                j -= 1
            bases.append(lo)
        if i < n - 1:
            j, lo = i + 1, v
            while j < n and values[j] <= v:
                lo = min(lo, values[j])
                j += 1
            bases.append(lo)
        if bases and v - max(bases) >= prominence:
            peaks.append(i)
    return peaks


def validate_beats(beats: Sequence[Beat], rules: dict, target_total: float | None = None,
                   durations: dict[str, float] | None = None) -> list[str]:
    p: list[str] = []
    if not beats:
        return ["beat sheet пуст"]
    spans = beat_spans(beats, durations)
    total = spans[-1][1]
    dur = [e - s for s, e in spans]

    if len(beats) < int(rules.get("min_beats", 7)):
        p.append(f"слишком мало блоков: {len(beats)}, нужно не меньше {rules.get('min_beats', 7)}")

    # 1. cold open
    co_max = float(rules.get("cold_open_max_s", 10))
    if beats[0].act != "cold_open":
        p.append(f"первый блок должен быть cold_open (сейчас {beats[0].act}): ролик начинается с самого "
                 f"сильного факта или образа, до любого представления")
    elif dur[0] > co_max + 0.5:
        p.append(f"cold_open длится {dur[0]:.1f} с, максимум {co_max:.0f} с — зритель решает остаться в "
                 f"первые секунды")

    # 2. open loop
    opened = {b.opens_loop: i for i, b in enumerate(beats) if b.opens_loop}
    closed = {b.closes_loop: i for i, b in enumerate(beats) if b.closes_loop}
    if not opened:
        p.append("нет open loop: ни один блок не заявляет вопрос (opens_loop), который закроется в конце")
    ol_max = float(rules.get("open_loop_max_start_s", 45))
    close_frac = float(rules.get("open_loop_close_min_frac", 0.66))
    if opened and min(spans[i][0] for i in opened.values()) > ol_max:
        p.append(f"open loop заявлен слишком поздно: позже {ol_max:.0f} с")
    for loop, i in opened.items():
        j = closed.get(loop)
        if j is None:
            p.append(f"петля {loop!r} открыта в {beats[i].block_id}, но нигде не закрыта (closes_loop)")
        elif j <= i:
            p.append(f"петля {loop!r} закрывается раньше, чем открывается")
        elif spans[j][0] < close_frac * total:
            p.append(f"петля {loop!r} закрыта слишком рано ({spans[j][0]:.0f} с из {total:.0f}); "
                     f"закрывать в последней трети")
    for loop in closed:
        if loop not in opened:
            p.append(f"closes_loop {loop!r} не соответствует ни одному opens_loop")

    # 3. pattern interrupt: длина блока и смена регистра
    max_beat = float(rules.get("max_beat_s", 40))
    for b, d in zip(beats, dur, strict=True):
        if d > max_beat + 0.5:
            p.append(f"блок {b.block_id} длится {d:.0f} с — без смены подачи не дольше {max_beat:.0f} с; раздели")
    for a, b in zip(beats, beats[1:], strict=False):
        if a.visual_register == b.visual_register:
            p.append(f"блоки {a.block_id} и {b.block_id} подряд в одном визуальном регистре "
                     f"{a.visual_register!r} — смени регистр, это pattern interrupt")

    # 4. callback
    last = beats[-1]
    if last.act != "finale":
        p.append(f"последний блок должен быть finale (сейчас {last.act})")
    if last.callback_to != beats[0].block_id:
        p.append(f"финал должен делать callback к cold_open: callback_to={beats[0].block_id!r}")

    # 5. кривая напряжения
    tens = [b.tension for b in beats]
    prom = float(rules.get("peak_prominence", 0.15))
    peaks = peak_indices(tens, prom)
    if len(peaks) < int(rules.get("min_peaks", 2)):
        p.append(f"кривая напряжения плоская: пиков {len(peaks)}, нужно не меньше {rules.get('min_peaks', 2)} "
                 f"с перепадом ≥{prom} и спадом между ними")
    top = max(tens)
    climax_idx = max(i for i, t in enumerate(tens) if t == top)
    if spans[climax_idx][0] < 0.5 * total:
        p.append("кульминация (максимум tension) должна быть во второй половине ролика")
    elif climax_idx > 0 and min(tens[:climax_idx]) > tens[climax_idx] - 0.2:
        p.append("перед кульминацией нужен спад напряжения — иначе пик не ощущается")

    # 6. длительность
    if target_total:
        tol = float(rules.get("duration_tolerance", 0.2))
        if abs(total - target_total) > tol * target_total:
            p.append(f"суммарная длительность {total:.0f} с, цель {target_total:.0f} с ±{tol:.0%}")
    return p


def act_sequence(beats: Sequence[Beat]) -> list[str]:
    return [b.act for b in beats]


def register_sequence(beats: Sequence[Beat]) -> list[str]:
    return [b.visual_register for b in beats]
