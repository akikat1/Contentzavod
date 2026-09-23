"""Обёртки над ffmpeg/ffprobe и работа с PCM.

Всё аудио внутри завода — моно 48 кГц float32 в numpy; на диск пишем WAV
16 бит. Так микширование, ducking и вставка пауз идут в Python без
многоэтажных filter_complex, которые на 16 ГБ ОЗУ уходят в OOM.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np

log = logging.getLogger("factory.media")
SR = 48000


class FFmpegError(RuntimeError):
    pass


def run(args: list[str], *, input_bytes: bytes | None = None, timeout: float | None = None,
        quiet: bool = True) -> subprocess.CompletedProcess:
    cmd = [args[0], "-hide_banner", "-nostdin", "-y", *args[1:]] if args[0] == "ffmpeg" else args
    if quiet and args[0] == "ffmpeg":
        cmd[1:1] = ["-loglevel", "error"]
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-2000:]
        raise FFmpegError(f"{' '.join(cmd[:12])}... → код {proc.returncode}\n{tail}")
    return proc


def run_stderr(args: list[str], timeout: float | None = None) -> str:
    """Запуск ffmpeg ради анализа (blackdetect, silencedetect, ebur128) — нужен stderr."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", *args[1:]] if args[0] == "ffmpeg" else args
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.decode("utf-8", "replace")[-2000:])
    return proc.stderr.decode("utf-8", "replace")


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, check=True,
    ).stdout
    return json.loads(out)


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


@lru_cache(maxsize=1)
def available_filters() -> frozenset[str]:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True).stdout.decode()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) == 3:
            names.add(parts[1])
    return frozenset(names)


# ---------------- PCM ----------------

def load_audio(path: Path, sr: int = SR) -> np.ndarray:
    """Любой формат → моно float32 [-1, 1]."""
    proc = run(["ffmpeg", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"])
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def save_wav(path: Path, pcm: np.ndarray, sr: int = SR) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(pcm, -1.0, 1.0)
    data = (clipped * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)
    return path


def silence(seconds: float, sr: int = SR) -> np.ndarray:
    return np.zeros(max(0, int(round(seconds * sr))), dtype=np.float32)


def apply_filter_chain(src: Path, dst: Path, chain: str, sr: int = SR) -> Path:
    """Прогнать файл через цепочку аудиофильтров ffmpeg (-af)."""
    if not chain:
        shutil.copyfile(src, dst)
        return dst
    run(["ffmpeg", "-i", str(src), "-af", chain, "-ac", "1", "-ar", str(sr), str(dst)])
    return dst


def db_to_gain(db: float) -> float:
    return float(10 ** (db / 20.0))


def rms_envelope(pcm: np.ndarray, sr: int = SR, win_s: float = 0.05) -> np.ndarray:
    """RMS по окнам, растянутый обратно до длины сигнала — для sidechain-ducking."""
    win = max(1, int(sr * win_s))
    n = len(pcm)
    if n == 0:
        return pcm.copy()
    pad = (-n) % win
    frames = np.pad(pcm, (0, pad)).reshape(-1, win)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return np.repeat(rms, win)[:n]


def trim_silence(pcm: np.ndarray, sr: int = SR, threshold_db: float = -45.0,
                 keep_s: float = 0.02) -> tuple[np.ndarray, float]:
    """Срезает тишину по краям синтезированной фразы. Возвращает (pcm, сколько срезано слева, с)."""
    if len(pcm) == 0:
        return pcm, 0.0
    thr = db_to_gain(threshold_db)
    idx = np.where(np.abs(pcm) > thr)[0]
    if len(idx) == 0:
        return pcm[:0], 0.0
    keep = int(keep_s * sr)
    a = max(0, idx[0] - keep)
    b = min(len(pcm), idx[-1] + keep)
    return pcm[a:b], a / sr
