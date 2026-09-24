"""Движки синтеза речи: edge-tts (основной), Piper и espeak-ng (офлайн-фолбэк).

Каждый движок возвращает WAV и пословные тайминги. edge-tts отдаёт настоящие
WordBoundary — WhisperX для него не нужен; для офлайн-движков тайминги
оцениваются пропорционально длине слов (потом их может уточнить faster-whisper).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ...core import media

log = logging.getLogger("factory.tts")


class TTSUnavailable(RuntimeError):
    pass


@dataclass
class Prosody:
    rate_pct: float = 0.0
    pitch_st: float = 0.0
    volume_pct: float = 0.0


@dataclass
class TTSResult:
    wav: Path
    duration: float
    words: list[tuple[str, float, float]]      # (слово, начало, конец) от начала файла
    engine: str


def proportional_words(text: str, duration: float, lead: float = 0.05, tail: float = 0.08
                       ) -> list[tuple[str, float, float]]:
    words = text.split()
    if not words:
        return []
    weights = [max(1, len(w.strip(".,!?:;«»\"—"))) + 1.5 for w in words]
    span = max(0.01, duration - lead - tail)
    t, out = lead, []
    total = sum(weights)
    for w, wt in zip(words, weights, strict=True):
        d = span * wt / total
        out.append((w, t, t + d * 0.92))
        t += d
    return out


class EdgeEngine:
    name = "edge"

    def __init__(self, timeout_s: int = 60):
        self.timeout_s = timeout_s

    def available(self) -> bool:
        try:
            import edge_tts  # noqa: F401, PLC0415
            return True
        except ImportError:
            return False

    @staticmethod
    def _connector():
        import aiohttp  # noqa: PLC0415
        cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        if cafile and Path(cafile).exists():
            return aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=cafile))
        return None

    def synthesize(self, text: str, voice: str, prosody: Prosody, base_f0_hz: float, out_wav: Path) -> TTSResult:
        import edge_tts  # noqa: PLC0415

        pitch_hz = base_f0_hz * (2 ** (prosody.pitch_st / 12) - 1)
        rate = f"{round(prosody.rate_pct):+d}%"
        pitch = f"{round(pitch_hz):+d}Hz"
        volume = f"{round(prosody.volume_pct):+d}%"

        async def _run() -> tuple[bytes, list]:
            comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume, boundary="WordBoundary",
                                        connector=self._connector(),
                                        proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
                                        receive_timeout=self.timeout_s)
            audio, bounds = bytearray(), []
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    bounds.append((chunk["text"], chunk["offset"] / 1e7, (chunk["offset"] + chunk["duration"]) / 1e7))
            return bytes(audio), bounds

        try:
            audio, bounds = asyncio.run(_run())
        except Exception as e:  # noqa: BLE001 — edge-tts бросает разнородные исключения сети
            raise TTSUnavailable(f"edge-tts: {type(e).__name__}: {e}") from e
        if not audio:
            raise TTSUnavailable("edge-tts: пустой аудиопоток")
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio)
        try:
            media.run(["ffmpeg", "-i", tmp.name, "-ac", "1", "-ar", str(media.SR), str(out_wav)])
        finally:
            os.unlink(tmp.name)
        dur = media.duration(out_wav)
        words = bounds or proportional_words(text, dur)
        return TTSResult(out_wav, dur, words, self.name)


class PiperEngine:
    name = "piper"

    def __init__(self, models_dir: Path):
        self.models_dir = models_dir

    def available(self) -> bool:
        return shutil.which("piper") is not None

    def synthesize(self, text: str, voice: str, prosody: Prosody, base_f0_hz: float, out_wav: Path) -> TTSResult:
        model = self.models_dir / f"{voice}.onnx"
        if not model.exists():
            raise TTSUnavailable(f"piper: нет модели {model}")
        length_scale = 1.0 / max(0.5, 1 + prosody.rate_pct / 100)
        raw = out_wav.with_suffix(".raw.wav")
        proc = subprocess.run(["piper", "--model", str(model), "--output_file", str(raw),
                               "--length_scale", f"{length_scale:.3f}"], input=text.encode(), capture_output=True)
        if proc.returncode != 0:
            raise TTSUnavailable(f"piper: {proc.stderr.decode()[-300:]}")
        chain = []
        if abs(prosody.pitch_st) > 0.1:
            factor = 2 ** (prosody.pitch_st / 12)
            chain.append(f"asetrate=22050*{factor:.4f},aresample={media.SR},atempo={1 / factor:.4f}")
        media.run(["ffmpeg", "-i", str(raw), *(["-af", ",".join(chain)] if chain else []), "-ac", "1",
                   "-ar", str(media.SR), str(out_wav)])
        raw.unlink(missing_ok=True)
        dur = media.duration(out_wav)
        return TTSResult(out_wav, dur, proportional_words(text, dur), self.name)


class EspeakEngine:
    """Роботизированный, но всегда доступный голос — дно, чтобы конвейер не вставал."""
    name = "espeak"

    def available(self) -> bool:
        return shutil.which("espeak-ng") is not None

    def synthesize(self, text: str, voice: str, prosody: Prosody, base_f0_hz: float, out_wav: Path) -> TTSResult:
        speed = int(160 * (1 + prosody.rate_pct / 100))
        pitch = int(max(0, min(99, 50 + prosody.pitch_st * 6)))
        raw = out_wav.with_suffix(".raw.wav")
        proc = subprocess.run(["espeak-ng", "-v", voice, "-s", str(speed), "-p", str(pitch), "-w", str(raw), text],
                              capture_output=True)
        if proc.returncode != 0:
            raise TTSUnavailable(f"espeak-ng: {proc.stderr.decode()[-300:]}")
        media.run(["ffmpeg", "-i", str(raw), "-ac", "1", "-ar", str(media.SR), str(out_wav)])
        raw.unlink(missing_ok=True)
        dur = media.duration(out_wav)
        return TTSResult(out_wav, dur, proportional_words(text, dur), self.name)
