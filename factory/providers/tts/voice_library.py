"""Библиотека голосов: config/voices/<lang>.yaml с разворачиванием derive_from."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...core.config import Config, load_yaml


@dataclass
class VoiceSpec:
    id: str
    engine: str
    voice: str
    roles: list[str]
    gender: str = "unknown"
    timbre: str = ""
    native: bool = True
    base_f0_hz: float = 150.0
    prosody: dict = field(default_factory=dict)
    fx_chain: str | None = None
    fallback: dict = field(default_factory=dict)
    derived_from: str | None = None

    @property
    def is_archive(self) -> bool:
        return self.fx_chain == "radio_1940"


class VoiceLibrary:
    def __init__(self, path: Path):
        raw = load_yaml(path)
        self.path = path
        self.defaults: dict = raw.get("defaults", {})
        entries = {v["id"]: v for v in raw.get("voices", [])}
        self.voices: dict[str, VoiceSpec] = {}
        for vid in entries:
            self.voices[vid] = self._resolve(vid, entries, set())

    @classmethod
    def for_config(cls, cfg: Config) -> VoiceLibrary:
        return cls(cfg.path("voice.library_dir", "config/voices") / f"{cfg.language}.yaml")

    def _resolve(self, vid: str, entries: dict, seen: set) -> VoiceSpec:
        if vid in seen:
            raise ValueError(f"Цикл derive_from в голосе {vid}")
        e = dict(entries[vid])
        parent_id = e.pop("derive_from", None)
        if parent_id:
            parent = self._resolve(parent_id, entries, seen | {vid})
            prosody = {**parent.prosody, **(e.get("prosody") or {})}
            return VoiceSpec(
                id=vid, engine=parent.engine, voice=parent.voice, roles=e.get("roles", parent.roles),
                gender=e.get("gender", parent.gender), timbre=e.get("timbre", parent.timbre),
                native=e.get("native", parent.native), base_f0_hz=e.get("base_f0_hz", parent.base_f0_hz),
                prosody=prosody, fx_chain=e.get("fx_chain", parent.fx_chain),
                fallback={**parent.fallback, **(e.get("fallback") or {})}, derived_from=parent_id)
        return VoiceSpec(id=vid, engine=e["engine"], voice=e["voice"], roles=e.get("roles", []),
                         gender=e.get("gender", "unknown"), timbre=e.get("timbre", ""),
                         native=bool(e.get("native", True)), base_f0_hz=float(e.get("base_f0_hz", 150)),
                         prosody=e.get("prosody") or {}, fx_chain=e.get("fx_chain"),
                         fallback=e.get("fallback") or {})

    def get(self, vid: str) -> VoiceSpec:
        if vid not in self.voices:
            raise KeyError(f"Голоса {vid!r} нет в {self.path.name}")
        return self.voices[vid]

    def for_role(self, role: str) -> list[VoiceSpec]:
        return [v for v in self.voices.values() if role in v.roles]

    def base_voice(self, v: VoiceSpec) -> str:
        """Идентификатор физической модели голоса: варианты одного голоса — это один «человек» для
        слуха при близкой просодии, поэтому для разных спикеров стараемся брать разные базовые."""
        return v.voice
