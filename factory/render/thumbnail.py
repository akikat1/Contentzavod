"""Обложка 1280×720: самый напряжённый кадр ролика + 2–4 крупных слова с обводкой."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance

from ..core import media
from .fonts import font


def frame_from(asset: str, at: float = 0.5) -> Image.Image:
    p = Path(asset)
    if p.suffix.lower() in (".mp4", ".mov", ".webm"):
        tmp = p.with_suffix(".thumb.png")
        media.run(["ffmpeg", "-ss", f"{at:.2f}", "-i", str(p), "-frames:v", "1", str(tmp)])
        img = Image.open(tmp).convert("RGB")
        tmp.unlink(missing_ok=True)
        return img
    return Image.open(p).convert("RGB")


def make_thumbnail(bg: Image.Image, text: str, out: Path, accent=(255, 215, 0), size=(1280, 720)) -> Path:
    W, H = size
    ar = bg.width / bg.height
    bg = bg.resize((int(H * ar), H) if ar > W / H else (W, int(W / ar)), Image.LANCZOS)
    bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    bg = ImageEnhance.Contrast(ImageEnhance.Color(bg).enhance(1.25)).enhance(1.15)
    shade = np.linspace(0.15, 0.85, W)[None, :, None]           # затемнение слева под текст
    arr = np.asarray(bg, dtype=np.float32) * (0.35 + 0.65 * shade)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    words = text.upper().split()
    lines = [" ".join(words[i:i + 2]) for i in range(0, len(words), 2)][:3]
    size_px = int(H * 0.2)
    while size_px > 40 and max(font(size_px).getlength(ln) for ln in lines) > W * 0.62:
        size_px = int(size_px * 0.92)
    f = font(size_px)
    y = (H - len(lines) * size_px * 1.1) / 2
    for k, ln in enumerate(lines):
        d.text((W * 0.05, y + k * size_px * 1.1), ln, font=f, fill=accent if k == len(lines) - 1 else (255, 255, 255),
               stroke_width=max(4, size_px // 12), stroke_fill=(0, 0, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return out
