"""Построители тестовых сценариев."""
from __future__ import annotations

from factory.core.schema import BeatSheet, Script
from factory.engagement.format_rotation import load_format
from factory.providers.llm import fixture as fx


def sheet(cfg, ct: str = "investigation", target: float = 420) -> BeatSheet:
    fmt = load_format(cfg, ct)
    return BeatSheet.model_validate(fx.beatsheet("Тестовая тема", ct, fmt["skeleton"], target, cfg.section("beatsheet")))


def script(cfg, ct: str = "investigation", target: float = 420, job_id: str = "job-test") -> Script:
    sh = sheet(cfg, ct, target)
    res = fx.research(sh.topic, "угол")
    return Script.model_validate(fx.script(job_id, sh.topic, ct, "ru", sh.model_dump(), res, 14.5))


def beat(bid: str, act: str, dur: float, tension: float, reg: str, **kw) -> dict:
    return {"block_id": bid, "act": act, "intent": "намерение", "target_duration_s": dur, "tension": tension,
            "visual_register": reg, **kw}


def seg(sid: str, bid: str, role: str, text: str, speaker: str | None = None) -> dict:
    return {"id": sid, "block_id": bid, "role": role, "speaker": speaker, "text": text,
            "visual": {"marker": "TEXT", "content": "x"}}
