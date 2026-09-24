"""Файловая раскладка джоба и политика очистки диска.

workspace/jobs/<job_id>/
  research.json beatsheet.json script.json voiceplan.json timeline.json shots.json
  audio/  (сегменты, voice.wav, mix.wav)   subs/   assets/   scenes/   out/
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import Config

KEEP_AFTER_PUBLISH = ("out", "script.json", "timeline.json", "shots.json", "metadata.json", "qa_report.json",
                      "research.json", "beatsheet.json", "voiceplan.json", "manifest.json")


def slugify(text: str, max_len: int = 40) -> str:
    table = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                          "abvgdeejzijklmnoprstufhccss_y_eua")
    s = text.lower().translate(table)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len].strip("-") or "topic"


def new_job_id(topic: str) -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(topic, 32)}"


class JobDir:
    def __init__(self, cfg: Config, job_id: str):
        self.job_id = job_id
        self.root = cfg.path("paths.workspace", "workspace") / "jobs" / job_id
        for sub in ("audio", "subs", "assets", "scenes", "out", "shorts"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def p(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def write_json(self, name: str, data: Any) -> Path:
        path = self.p(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, BaseModel):
            text = data.model_dump_json(indent=2)
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return path

    def read_json(self, name: str) -> Any:
        return json.loads(self.p(name).read_text(encoding="utf-8"))

    def exists(self, name: str) -> bool:
        return self.p(name).exists()


def _json_default(o: Any) -> Any:
    if isinstance(o, BaseModel):
        return o.model_dump()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))


def cleanup_intermediates(cfg: Config, job_id: str) -> int:
    """После публикации оставляем финалы и манифесты; промежутки (3–5 ГБ) удаляем."""
    root = cfg.path("paths.workspace", "workspace") / "jobs" / job_id
    freed = 0
    if not root.exists():
        return 0
    for child in root.iterdir():
        if child.name in KEEP_AFTER_PUBLISH or child.name == "shorts":
            continue
        freed += _size(child)
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    return freed


def enforce_history_limit(cfg: Config, keep: int) -> list[str]:
    jobs_dir = cfg.path("paths.workspace", "workspace") / "jobs"
    if not jobs_dir.exists():
        return []
    jobs = sorted((d for d in jobs_dir.iterdir() if d.is_dir()), key=lambda d: d.name)
    removed = []
    for d in jobs[:-keep] if keep > 0 else []:
        shutil.rmtree(d)
        removed.append(d.name)
    return removed


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
