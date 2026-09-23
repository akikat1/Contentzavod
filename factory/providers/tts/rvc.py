"""Слой 4: RVC v2 — перенос тембра конкретного человека на синтезированную речь.

Работает через внешний CLI (по умолчанию `rvc-python`), чтобы тяжёлые
зависимости (torch, fairseq) не попадали в основной пакет. Аудио режется на
чанки: на 4 ГБ VRAM длинный файл целиком не помещается. Модель тренируется
только на голосе, права на который есть.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

import numpy as np

from ...core import media

log = logging.getLogger("factory.rvc")


class RVCUnavailable(RuntimeError):
    pass


def convert(src: Path, dst: Path, command: str, model: Path, chunk_s: float = 30.0) -> Path:
    if not model.exists():
        raise RVCUnavailable(f"нет RVC-модели {model} — положите .pth (и .index) в assets/rvc/")
    pcm = media.load_audio(src)
    chunk = int(chunk_s * media.SR)
    out_parts = []
    work = dst.parent / (dst.stem + "_rvc")
    work.mkdir(parents=True, exist_ok=True)
    for i, start in enumerate(range(0, len(pcm), chunk)):
        part_in = media.save_wav(work / f"in_{i:03d}.wav", pcm[start:start + chunk])
        part_out = work / f"out_{i:03d}.wav"
        cmd = command.format(input=shlex.quote(str(part_in)), output=shlex.quote(str(part_out)),
                             model=shlex.quote(str(model)))
        proc = subprocess.run(cmd, shell=True, capture_output=True, timeout=1800)
        if proc.returncode != 0 or not part_out.exists():
            raise RVCUnavailable(f"RVC упал на чанке {i}: {proc.stderr.decode()[-400:]}")
        converted = media.load_audio(part_out)
        n = min(len(converted), len(pcm[start:start + chunk]))   # длительность должна сохраниться
        out_parts.append(converted[:n])
    media.save_wav(dst, np.concatenate(out_parts) if out_parts else pcm)
    return dst
