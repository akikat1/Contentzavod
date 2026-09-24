"""Visual Rhythm: темп монтажа подчинён смыслу, а не фиксированным N секундам.

- длительность плана — от акта и напряжения блока (перечисление быстро, объяснение
  спокойно, кульминация — один длинный план);
- зум-панч на словах в [emph], счётчик на акцентных числах;
- цветокоррекция следует за напряжением и регистром (архив — сепия и зерно).
"""
from __future__ import annotations

import re

NUM_RE = re.compile(r"^[«(]?(\d[\d\s .,]*)(%|[^\d\s]*)$")


def shot_length(act: str, tension: float, cfg_shot: dict) -> float:
    if act == "climax":
        return float(cfg_shot.get("climax", 9.0))
    if act == "cold_open":
        return float(cfg_shot.get("cold_open", 2.4))
    if act == "list_item":
        return float(cfg_shot.get("list_item", 2.2))
    calm, tense = float(cfg_shot.get("calm", 7.0)), float(cfg_shot.get("tense", 2.5))
    return calm + (tense - calm) * max(0.0, min(1.0, tension))


def split_durations(total: float, target: float, min_len: float) -> list[float]:
    if total <= 0:
        return []
    n = max(1, round(total / max(target, min_len)))
    while n > 1 and total / n < min_len:
        n -= 1
    return [total / n] * n


def grade_for(act: str, tension: float, register: str) -> dict:
    g = {"contrast": 1.0 + 0.14 * tension, "saturation": 1.08 - 0.22 * max(0.0, tension - 0.5),
         "brightness": -0.02 * tension, "gamma": 1.0, "warmth": 0.035 * (0.5 - tension)}
    if register == "archive":
        g.update(saturation=0.0, sepia=True, grain=12, vignette=True, contrast=1.12)
    elif tension >= 0.85 or act == "climax":
        g["vignette"] = True
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in g.items()}


def parse_number(word: str) -> tuple[float, str] | None:
    """«2224», «1 500», «46%» → (значение, суффикс). Годы и проценты тоже числа."""
    w = word.strip(".,!?:;»\"")
    m = NUM_RE.match(w)
    if not m:
        return None
    digits = re.sub(r"[\s ]", "", m.group(1)).replace(",", ".").rstrip(".")
    try:
        val = float(digits)
    except ValueError:
        return None
    suffix = m.group(2) or ""
    return val, (suffix if suffix in ("%", "°") else "")
