"""Выбор движка для голоса с фолбэком и кэшем синтеза (повторный прогон стадии бесплатен)."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

from ...core.config import Config
from .engines import EdgeEngine, EspeakEngine, PiperEngine, Prosody, TTSResult, TTSUnavailable
from .voice_library import VoiceSpec

log = logging.getLogger("factory.tts")


class TTSRouter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache = cfg.path("paths.cache", "assets/cache") / "tts"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.engines = {"edge": EdgeEngine(), "piper": PiperEngine(cfg.path("paths.assets", "assets") / "piper"),
                        "espeak": EspeakEngine()}
        self.down: set[str] = set()
        self.used: dict[str, int] = {}

    def chain(self, voice: VoiceSpec) -> list[tuple[str, str]]:
        order = [voice.engine] + [e for e in self.cfg.get("voice.engines_fallback", ["edge", "piper", "espeak"])
                                  if e != voice.engine]
        out = []
        for eng in order:
            if self.cfg.offline and eng == "edge":
                continue
            name = voice.voice if eng == voice.engine else voice.fallback.get(eng)
            if name:
                out.append((eng, name))
        return out

    def synthesize(self, text: str, voice: VoiceSpec, prosody: Prosody, out_wav: Path) -> TTSResult:
        errors = []
        for eng, name in self.chain(voice):
            if eng in self.down or not self.engines[eng].available():
                continue
            key = hashlib.sha256(json.dumps([eng, name, text, round(prosody.rate_pct, 1),
                                             round(prosody.pitch_st, 2), round(prosody.volume_pct, 1)],
                                            ensure_ascii=False).encode()).hexdigest()[:24]
            cw, cj = self.cache / f"{key}.wav", self.cache / f"{key}.json"
            if cw.exists() and cj.exists():
                meta = json.loads(cj.read_text(encoding="utf-8"))
                shutil.copyfile(cw, out_wav)
                self.used[eng] = self.used.get(eng, 0) + 1
                return TTSResult(out_wav, meta["duration"], [tuple(w) for w in meta["words"]], eng)
            try:
                res = self.engines[eng].synthesize(text, name, prosody, voice.base_f0_hz, out_wav)
            except TTSUnavailable as e:
                errors.append(str(e))
                log.warning("%s недоступен: %s", eng, e)
                if eng == "edge" and ("403" in str(e) or "connect" in str(e).lower()):
                    self.down.add(eng)          # сеть/блокировка — не долбим каждую фразу
                continue
            shutil.copyfile(out_wav, cw)
            cj.write_text(json.dumps({"duration": res.duration, "words": res.words}, ensure_ascii=False),
                          encoding="utf-8")
            self.used[eng] = self.used.get(eng, 0) + 1
            return res
        raise TTSUnavailable(f"ни один движок не озвучил голос {voice.id}: {errors[-3:]}")
