"""Гуманизация голоса: FX-цепочки ролей, пофразная просодия, вдохи, комната, постобработка.

Слой 1 (текст под ухо) живёт в промте сценария, слои 2–3 — здесь, слой 4 (RVC) — в rvc.py.
"""
from __future__ import annotations

import hashlib
import random
from pathlib import Path

import numpy as np

from ...core import media
from .engines import Prosody
from .markup import Unit

FX_CHAINS = {
    # архивная радиопередача: узкая полоса, сжатие, короткое эхо студии
    "radio_1940": "highpass=f=320,lowpass=f=3300,acompressor=threshold=-22dB:ratio=6:attack=4:release=60,"
                  "aecho=0.7:0.35:18:0.22,volume=1.5",
    "phone": "highpass=f=420,lowpass=f=3000,acompressor=threshold=-18dB:ratio=8:attack=3:release=40,volume=1.35",
    "inner_thought": "aecho=0.85:0.75:70|140:0.35|0.18,lowpass=f=6500,volume=0.95",
    "quote_room": "aecho=0.8:0.55:28:0.22",
    "aged": "lowpass=f=7200,vibrato=f=5.5:d=0.06",
}
NOISY_FX = {"radio_1940": -40.0, "phone": -48.0}     # добавить шум эфира, дБ


def parse_pct(v: str | None) -> float:
    return float(v.rstrip("%")) if v else 0.0


def parse_pitch_st(v: str | None, base_f0: float) -> float:
    if not v:
        return 0.0
    if v.endswith("st"):
        return float(v[:-2])
    hz = float(v[:-2])
    return 12 * np.log2(max(1e-3, (base_f0 + hz) / base_f0))


def unit_prosody(voice_prosody: dict, seg_prosody: dict | None, unit: Unit, base_f0: float, jitter: dict,
                 seed: str, humanize: bool) -> Prosody:
    """Слой 2: голос (вариант) + указание сценария + пофразная вариация, привязанная к смыслу."""
    seg_prosody = seg_prosody or {}
    rate = parse_pct(voice_prosody.get("rate")) + parse_pct(seg_prosody.get("rate"))
    pitch = parse_pitch_st(voice_prosody.get("pitch"), base_f0) + parse_pitch_st(seg_prosody.get("pitch"), base_f0)
    vol = parse_pct(voice_prosody.get("volume")) + parse_pct(seg_prosody.get("volume"))
    if unit.slow:
        rate -= 12
    if humanize:
        rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16))
        rate += rng.uniform(-1, 1) * float(jitter.get("rate_pct", 4))
        pitch += rng.uniform(-1, 1) * float(jitter.get("pitch_st", 2)) * 0.5
        if unit.is_question:
            pitch += 1.2          # вопрос звучит выше
        elif unit.ends_sentence and len(unit.words) > 10:
            rate -= 2             # длинный вывод — чуть медленнее
    return Prosody(rate_pct=max(-40, min(40, rate)), pitch_st=max(-6, min(6, pitch)), volume_pct=vol)


def apply_fx(src: Path, dst: Path, fx: str | None, seed: int = 0) -> Path:
    if not fx or fx not in FX_CHAINS:
        return media.apply_filter_chain(src, dst, "")
    media.apply_filter_chain(src, dst, FX_CHAINS[fx])
    if fx in NOISY_FX:
        pcm = media.load_audio(dst)
        rng = np.random.default_rng(seed)
        noise = rng.standard_normal(len(pcm)).astype(np.float32) * media.db_to_gain(NOISY_FX[fx])
        media.save_wav(dst, pcm + noise)
    return dst


def emphasize(pcm: np.ndarray, spans: list[tuple[float, float]], gain_db: float = 2.0) -> np.ndarray:
    """Акцент без разреза синтеза: плавный подъём громкости на отрезке слова."""
    out = pcm.copy()
    g = media.db_to_gain(gain_db)
    ramp = int(0.02 * media.SR)
    for a, b in spans:
        i, j = int(a * media.SR), int(b * media.SR)
        if j <= i or i >= len(out):
            continue
        j = min(j, len(out))
        env = np.full(j - i, g, dtype=np.float32)
        r = min(ramp, (j - i) // 2)
        if r:
            env[:r] = np.linspace(1, g, r)
            env[-r:] = np.linspace(g, 1, r)
        out[i:j] *= env
    return out


def convolve_room(pcm: np.ndarray, ir: np.ndarray, wet: float) -> np.ndarray:
    """Слой 3: реверберация комнаты свёрткой (overlap-add блоками — память не растёт с длиной ролика)."""
    if wet <= 0 or len(ir) == 0 or len(pcm) == 0:
        return pcm
    block = 1 << 18
    nfft = 1 << int(np.ceil(np.log2(block + len(ir) - 1)))
    irf = np.fft.rfft(ir, nfft)
    out = np.zeros(len(pcm) + len(ir) - 1, dtype=np.float32)
    for start in range(0, len(pcm), block):
        chunk = pcm[start:start + block]
        y = np.fft.irfft(np.fft.rfft(chunk, nfft) * irf, nfft)[: len(chunk) + len(ir) - 1]
        out[start:start + len(y)] += y.astype(np.float32)
    wet_sig = out[: len(pcm)]
    rms_d = float(np.sqrt(np.mean(pcm ** 2))) or 1.0
    rms_w = float(np.sqrt(np.mean(wet_sig ** 2))) or 1.0
    return (pcm + wet * wet_sig * (rms_d / rms_w)).astype(np.float32)


def post_process(src: Path, dst: Path, chain: str, lufs: float) -> Path:
    full = ",".join(c for c in (chain, f"loudnorm=I={lufs}:TP=-1.5:LRA=11") if c)
    return media.apply_filter_chain(src, dst, full)


class BreathBank:
    def __init__(self, folder: Path):
        self.samples = [media.load_audio(p) for p in sorted(folder.glob("*.wav"))] if folder.exists() else []

    def pick(self, rng: random.Random, gain_db: float) -> np.ndarray | None:
        if not self.samples:
            return None
        return self.samples[rng.randrange(len(self.samples))] * media.db_to_gain(gain_db)
