"""Rutube: загрузка по публичной ссылке на файл (поэтому нужен буфер R2)."""
from __future__ import annotations

import time

import httpx

from .base import Deferred, PostMeta, PostResult, Publisher, PublishError, Video


class RutubePublisher(Publisher):
    name = "rutube"
    needs_public_url = True

    def publish(self, video: Video, meta: PostMeta) -> PostResult:
        if not video.public_url:
            raise PublishError("rutube: нужен публичный URL файла — включите publish.remote_storage (R2)")
        r = httpx.post("https://rutube.ru/api/video/", timeout=120,
                       headers={"Authorization": f"Token {self.env('token_env')}"},
                       json={"url": video.public_url, "title": meta.title[:100], "description": meta.description[:5000],
                             "is_hidden": False, "category_id": int(self.pc.get("category_id", 13))})
        if r.status_code == 429:
            raise Deferred("rutube: лимит", time.time() + 3600)
        if r.status_code >= 400:
            raise PublishError(f"rutube {r.status_code}: {r.text[:200]}")
        vid = r.json().get("video_id") or r.json().get("id")
        return PostResult(url=f"https://rutube.ru/video/{vid}/", platform_id=str(vid))
