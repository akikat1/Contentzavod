"""Контекст стадии: конфиг, БД, пул ключей, LLM, папка джоба, промты, фидбэк ремонта."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from .config import Config, load_yaml
from .db import DB, now
from .keypool import KeyPool
from .workspace import JobDir

LANG_NAMES = {"ru": "русский", "en": "английский (English)"}


@dataclass
class Ctx:
    cfg: Config
    db: DB
    job_id: str
    pool: KeyPool | None = None
    notes: list[str] = field(default_factory=list)

    @cached_property
    def job(self) -> JobDir:
        return JobDir(self.cfg, self.job_id)

    @cached_property
    def log(self) -> logging.Logger:
        return logging.getLogger(f"factory.job.{self.job_id[-24:]}")

    @cached_property
    def llm(self):
        from ..providers.llm.router import LLMRouter  # noqa: PLC0415 — избегаем цикла импорта
        return LLMRouter(self.cfg, self.pool)

    @cached_property
    def library(self):
        from ..providers.tts.voice_library import VoiceLibrary  # noqa: PLC0415
        return VoiceLibrary.for_config(self.cfg)

    @cached_property
    def niche(self) -> dict:
        return load_yaml(self.cfg.path("channel.niche_file", "config/niches/example.yaml"))

    # ---------------- метаданные джоба ----------------
    def meta(self) -> dict:
        row = self.db.one("SELECT meta FROM jobs WHERE job_id=?", (self.job_id,))
        return json.loads(row["meta"]) if row else {}

    def update_meta(self, **kv: Any) -> None:
        m = self.meta()
        m.update(kv)
        self.db.execute("UPDATE jobs SET meta=?, updated_at=? WHERE job_id=?",
                        (json.dumps(m, ensure_ascii=False), now(), self.job_id))

    def mark_synthetic(self, why: str) -> None:
        """Контент из офлайн-фикстуры никогда не уходит на реальные площадки."""
        m = self.meta()
        reasons = set(m.get("synthetic", []))
        reasons.add(why)
        self.update_meta(synthetic=sorted(reasons))

    @property
    def synthetic(self) -> bool:
        return bool(self.meta().get("synthetic"))

    # ---------------- промты ----------------
    def prompt(self, name: str, **vars_: Any) -> str:
        path = self.cfg.root / "prompts" / f"{name}.md"
        text = path.read_text(encoding="utf-8")

        def sub(m: re.Match) -> str:
            key = m.group(1)
            if key not in vars_:
                raise KeyError(f"В промте {name}.md не передана переменная {{{{{key}}}}}")
            val = vars_[key]
            return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, indent=1)
        return re.sub(r"\{\{(\w+)\}\}", sub, text)

    def system_prompt(self) -> str:
        return self.prompt(
            "system", channel_name=self.cfg.get("channel.name", ""), niche=self.niche.get("niche", ""),
            audience=self.niche.get("audience", ""), tone=self.niche.get("tone", ""),
            language_name=LANG_NAMES.get(self.cfg.language, self.cfg.language))

    # ---------------- фидбэк авто-ремонта ----------------
    def repair_feedback(self, stage: str) -> str:
        path = self.job.p("repair_feedback.json")
        if not path.exists():
            return ""
        items = json.loads(path.read_text(encoding="utf-8")).get(stage, [])
        if not items:
            return ""
        return ("<repair_feedback>\nПредыдущая версия не прошла автоматическую приёмку. Исправь:\n"
                + "\n".join(f"- {t}" for t in items) + "\n</repair_feedback>")

    def artifact(self, name: str) -> Path:
        return self.job.p(name)
