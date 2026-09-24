"""Процедурные аудиоассеты: вдохи, импульс комнаты, SFX, музыкальные подложки.

Генерируются кодом при `factory init`, поэтому в репозитории нет чужих файлов
с неясной лицензией, а конвейер работает даже с пустой папкой assets/.
Живая музыка из assets/music/<mood>/ всегда предпочтительнее процедурной.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ...core import media

SR = media.SR


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _bandpass_noise(n: int, lo: float, hi: float, rng: np.random.Generator) -> np.ndarray:
    spec = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / SR)
    mask = ((freqs >= lo) & (freqs <= hi)).astype(float)
    return np.fft.irfft(spec * mask, n).astype(np.float32)


def _norm(x: np.ndarray, peak: float = 0.9) -> np.ndarray:
    m = float(np.max(np.abs(x))) or 1.0
    return (x / m * peak).astype(np.float32)


def breath(seed: int, dur: float = 0.42) -> np.ndarray:
    """Вдох: полосовой шум 350–2800 Гц с мягкой атакой и спадом."""
    rng = _rng(seed)
    n = int(dur * SR)
    x = _bandpass_noise(n, 350, 2800 + 400 * rng.random(), rng)
    t = np.linspace(0, 1, n)
    env = np.sin(np.pi * np.clip(t * (1.0 + 0.3 * rng.random()), 0, 1)) ** 1.6
    return _norm(x * env, 0.5)


def room_ir(rt60: float = 0.35, seed: int = 7) -> np.ndarray:
    """Импульс небольшой комнаты: ранние отражения + экспоненциальный хвост."""
    rng = _rng(seed)
    n = int(rt60 * 1.3 * SR)
    t = np.arange(n) / SR
    tail = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    tail = np.convolve(tail, np.ones(8) / 8, mode="same")        # мягче верх
    ir = np.zeros(n, dtype=np.float64)
    ir[0] = 1.0
    for d, g in ((0.007, 0.5), (0.011, 0.42), (0.017, 0.35), (0.023, 0.28), (0.031, 0.2)):
        ir[int(d * SR)] += g * (1 if rng.random() > 0.5 else -1)
    ir += 0.25 * tail
    return (ir / np.sqrt(np.sum(ir ** 2))).astype(np.float32)


def whoosh(seed: int = 1, dur: float = 0.7) -> np.ndarray:
    rng = _rng(seed)
    n = int(dur * SR)
    frames, hop = [], 1024
    noise = rng.standard_normal(n + 4096)
    out = np.zeros(n, dtype=np.float32)
    win = np.hanning(2048)
    for i, start in enumerate(range(0, n - 2048, hop)):
        frac = start / n
        center = 300 + 5000 * np.sin(np.pi * frac)
        seg = noise[start:start + 2048] * win
        spec = np.fft.rfft(seg)
        f = np.fft.rfftfreq(2048, 1 / SR)
        spec *= np.exp(-((f - center) / (center * 0.6)) ** 2)
        out[start:start + 2048] += np.fft.irfft(spec, 2048).astype(np.float32)
        frames.append(i)
    env = np.sin(np.pi * np.linspace(0, 1, n)) ** 2
    return _norm(out * env, 0.8)


def impact(seed: int = 2, dur: float = 1.4) -> np.ndarray:
    rng = _rng(seed)
    n = int(dur * SR)
    t = np.arange(n) / SR
    freq = 38 + 50 * np.exp(-t * 9)
    body = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 3.2)
    click = _bandpass_noise(n, 800, 6000, rng) * np.exp(-t * 60)
    return _norm(body + 0.35 * click, 0.95)


def riser(seed: int = 3, dur: float = 1.6) -> np.ndarray:
    rng = _rng(seed)
    n = int(dur * SR)
    t = np.arange(n) / SR
    freq = 180 * (7 ** (t / dur))
    tone = np.sin(2 * np.pi * np.cumsum(freq) / SR)
    noise = _bandpass_noise(n, 1500, 9000, rng)
    env = (t / dur) ** 2.2
    return _norm((0.6 * tone + 0.5 * noise) * env, 0.8)


def ambience(seed: int = 4, dur: float = 12.0) -> np.ndarray:
    rng = _rng(seed)
    n = int(dur * SR)
    brown = np.cumsum(rng.standard_normal(n))
    brown -= np.convolve(brown, np.ones(4800) / 4800, mode="same")   # убрать дрейф
    x = _norm(brown, 0.6)
    fade = int(0.5 * SR)
    x[:fade] *= np.linspace(0, 1, fade)
    x[-fade:] *= np.linspace(1, 0, fade)
    return x


CHORDS = {   # частоты в Гц: простые, но различимые по настроению аккорды
    "tense": [110.0, 116.54, 164.81, 220.0],
    "calm": [130.81, 164.81, 196.0, 246.94],
    "build": [146.83, 220.0, 293.66, 349.23],
    "epic": [73.42, 110.0, 146.83, 220.0, 293.66],
    "reflective": [174.61, 220.0, 261.63, 329.63],
    "neutral": [196.0, 220.0, 293.66, 392.0],
    "dark": [65.41, 92.5, 130.81, 185.0],
    "uplifting": [130.81, 196.0, 261.63, 329.63, 392.0],
}


def music_pad(mood: str, dur: float, seed: int = 5) -> np.ndarray:
    """Процедурная подложка: аддитивный синтез аккорда + медленное дыхание громкости.
    Для «build» — пульсация восьмыми с нарастанием, для «tense» — биения."""
    rng = _rng(seed + hash(mood) % 1000)
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = np.zeros(n)
    for i, f in enumerate(CHORDS.get(mood, CHORDS["neutral"])):
        detune = 1 + 0.002 * (rng.random() - 0.5)
        ph = rng.random() * 2 * np.pi
        for h, g in ((1, 1.0), (2, 0.35), (3, 0.12)):
            x += g / (1 + i * 0.3) * np.sin(2 * np.pi * f * h * detune * t + ph)
    lfo = 0.75 + 0.25 * np.sin(2 * np.pi * t / (7 + 3 * rng.random()))
    x *= lfo
    if mood == "build":
        pulse = 0.55 + 0.45 * (np.sin(2 * np.pi * 2.0 * t) > 0)
        x *= pulse * np.linspace(0.6, 1.0, n)
    fade = min(n // 4, int(2 * SR))
    if fade:
        x[:fade] *= np.linspace(0, 1, fade)
        x[-fade:] *= np.linspace(1, 0, fade)
    return _norm(x, 0.7)


SFX = {"whoosh": whoosh, "impact": impact, "riser": riser, "ambience": ambience}


def bootstrap(assets_generated: Path) -> list[Path]:
    """Создать все процедурные ассеты. Идемпотентно."""
    made = []
    bdir = assets_generated / "breaths"
    for i in range(8):
        p = bdir / f"breath_{i:02d}.wav"
        if not p.exists():
            media.save_wav(p, breath(100 + i, 0.34 + 0.03 * i))
            made.append(p)
    irp = assets_generated / "ir" / "room_small.wav"
    if not irp.exists():
        media.save_wav(irp, room_ir())
        made.append(irp)
    for name, fn in SFX.items():
        p = assets_generated / "sfx" / f"{name}.wav"
        if not p.exists():
            media.save_wav(p, fn())
            made.append(p)
    return made
