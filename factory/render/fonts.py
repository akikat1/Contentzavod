"""Поиск TTF-шрифта с кириллицей для PIL и drawtext."""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


@lru_cache(maxsize=8)
def font_path(family: str = "DejaVu Sans", bold: bool = True) -> str:
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", f"{family}:{'bold' if bold else 'regular'}"],
                             capture_output=True, timeout=5).stdout.decode().strip()
        if out and Path(out).exists():
            return out
    except (OSError, subprocess.TimeoutExpired):
        pass
    for p in FALLBACKS:
        if Path(p).exists():
            return p
    raise FileNotFoundError("Не найден TTF-шрифт с кириллицей: установите fonts-dejavu-core")


@lru_cache(maxsize=64)
def font(size: int, family: str | bool = "DejaVu Sans", bold: bool = True) -> ImageFont.FreeTypeFont:
    if isinstance(family, bool):          # font(size, False) — обычное начертание семейства по умолчанию
        family, bold = "DejaVu Sans", family
    return ImageFont.truetype(font_path(family, bold), size)
