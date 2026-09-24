"""Реестр адаптеров площадок."""
from __future__ import annotations

from ...core.config import Config
from .base import Publisher
from .bluesky import BlueskyPublisher
from .meta import FacebookPublisher, InstagramPublisher
from .postiz import PostizPublisher
from .rutube import RutubePublisher
from .sandbox import SandboxPublisher
from .telegram import TelegramPublisher
from .tiktok import TikTokPublisher
from .vk import VKPublisher
from .youtube import YouTubePublisher

PUBLISHERS: dict[str, type[Publisher]] = {
    "youtube": YouTubePublisher, "telegram": TelegramPublisher, "bluesky": BlueskyPublisher, "vk": VKPublisher,
    "rutube": RutubePublisher, "tiktok": TikTokPublisher, "instagram": InstagramPublisher,
    "facebook": FacebookPublisher, "postiz": PostizPublisher,
}


def get_publisher(cfg: Config, platform: str, sandbox: bool) -> Publisher:
    if sandbox:
        return SandboxPublisher(cfg, platform)
    return PUBLISHERS[platform](cfg)


def enabled_platforms(cfg: Config, include_disabled_in_sandbox: bool = False) -> dict[str, dict]:
    plats = cfg.section("publish.platforms")
    return {n: p for n, p in plats.items() if n in PUBLISHERS and (p.get("enabled") or include_disabled_in_sandbox)}
