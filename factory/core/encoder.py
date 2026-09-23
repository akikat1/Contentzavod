"""Выбор видеокодека.

GTX 1650 первой ревизии (TU117) несёт энкодер поколения Volta: H.264 есть,
HEVC B-frames нет. Поэтому только h264_nvenc, и только после проверки
реальным кадром — наличие энкодера в `ffmpeg -encoders` не гарантирует,
что драйвер его отдаст.
"""
from __future__ import annotations

import logging
import subprocess
from functools import lru_cache

log = logging.getLogger("factory.encoder")


@lru_cache(maxsize=4)
def nvenc_works() -> bool:
    try:
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, timeout=20).stdout
        if b"h264_nvenc" not in enc:
            return False
        test = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.2",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=30,
        )
        return test.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def video_codec_args(mode: str = "auto", quality: str = "normal", fps: int = 30) -> list[str]:
    """Аргументы кодирования. Одинаковые для всех сцен — иначе concat без перекодирования сломается."""
    gop = str(fps * 2)
    use_nvenc = mode == "nvenc" or (mode == "auto" and nvenc_works())
    if use_nvenc:
        cq = {"draft": "32", "normal": "23", "high": "19"}[quality]
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", cq, "-b:v", "0",
                "-g", gop, "-bf", "0", "-pix_fmt", "yuv420p", "-profile:v", "high"]
    crf = {"draft": "30", "normal": "21", "high": "18"}[quality]
    preset = {"draft": "ultrafast", "normal": "veryfast", "high": "medium"}[quality]
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-g", gop, "-bf", "0",
            "-pix_fmt", "yuv420p", "-profile:v", "high"]


def describe(mode: str = "auto") -> str:
    if mode == "nvenc" or (mode == "auto" and nvenc_works()):
        return "h264_nvenc"
    return "libx264"
