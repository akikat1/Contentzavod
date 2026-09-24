"""Песочница: dry-run и синтетический контент. Ничего не уходит наружу, результат пишется в out/published/."""
from __future__ import annotations

import json
import time

from .base import PostMeta, PostResult, Publisher, Video


class SandboxPublisher(Publisher):
    def __init__(self, cfg, platform: str):
        self.name = platform
        super().__init__(cfg)

    def publish(self, video: Video, meta: PostMeta) -> PostResult:
        out = video.path.parent.parent / "out" / "published"
        out.mkdir(parents=True, exist_ok=True)
        pid = f"sandbox-{self.name}-{video.variant}-{int(time.time())}"
        (out / f"{self.name}_{video.variant}.json").write_text(json.dumps({
            "platform": self.name, "variant": video.variant, "file": str(video.path), "duration": video.duration,
            "size": f"{video.width}x{video.height}", "title": meta.title, "description": meta.description,
            "tags": meta.tags, "hashtags": meta.hashtags}, ensure_ascii=False, indent=2), encoding="utf-8")
        return PostResult(url=f"sandbox://{self.name}/{pid}", platform_id=pid)
