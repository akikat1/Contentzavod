"""Контракт адаптера площадки: publish(video, meta) -> PostResult.
Новая площадка = один файл с классом-наследником и строка в config/factory.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ...core.config import Config


class PublishError(RuntimeError):
    """Постоянная ошибка: неверные данные, отказ площадки. Повтор не поможет без исправления."""


class Deferred(RuntimeError):
    """Временный отказ (лимит, 429, обработка видео): повторить не раньше retry_at."""

    def __init__(self, message: str, retry_at: float):
        super().__init__(message)
        self.retry_at = retry_at


@dataclass
class Video:
    path: Path
    variant: str                  # master | short_01 ...
    duration: float
    width: int
    height: int
    thumbnail: Path | None = None
    captions: Path | None = None
    public_url: str | None = None


@dataclass
class PostMeta:
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)


@dataclass
class PostResult:
    url: str
    platform_id: str
    extra: dict = field(default_factory=dict)


class Publisher:
    name = "base"
    needs_public_url = False

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pc = cfg.section(f"publish.platforms.{self.name}")

    def env(self, key: str, required: bool = True) -> str:
        var = self.pc.get(key)
        val = os.environ.get(var or "", "")
        if required and not val:
            raise PublishError(f"{self.name}: не задана переменная окружения {var} ({key})")
        return val

    def publish(self, video: Video, meta: PostMeta) -> PostResult:  # pragma: no cover — интерфейс
        raise NotImplementedError
