"""Postiz (self-hosted, AGPL-3.0): один контейнер вместо отдельных интеграций X, LinkedIn,
Pinterest, Threads, Reddit, Mastodon и др. Площадки подключаются в UI Postiz один раз,
сюда вписываются id интеграций. Схему публичного API сверяйте с docs.postiz.com вашей версии."""
from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx

from .base import Deferred, PostMeta, PostResult, Publisher, PublishError, Video


class PostizPublisher(Publisher):
    name = "postiz"

    def publish(self, video: Video, meta: PostMeta) -> PostResult:
        base = self.pc.get("base_url", "http://127.0.0.1:5000/public/v1").rstrip("/")
        integrations = self.pc.get("integrations") or []
        if not integrations:
            raise PublishError("postiz: не указаны integrations (id каналов из UI Postiz)")
        h = {"Authorization": self.env("api_key_env")}
        with open(video.path, "rb") as fh:
            up = httpx.post(f"{base}/upload", headers=h, files={"file": (video.path.name, fh, "video/mp4")},
                            timeout=1800)
        if up.status_code == 429:
            raise Deferred("postiz: лимит", time.time() + 3600)
        if up.status_code >= 400:
            raise PublishError(f"postiz upload {up.status_code}: {up.text[:200]}")
        media = up.json()
        content = f"{meta.title}\n\n{meta.description}\n\n{' '.join(meta.hashtags)}".strip()
        body = {"type": "now", "shortLink": False, "tags": [],
                "date": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "posts": [{"integration": {"id": iid},
                           "value": [{"content": content[:2800],
                                      "image": [{"id": media.get("id"), "path": media.get("path")}]}],
                           "settings": {}} for iid in integrations]}
        r = httpx.post(f"{base}/posts", headers=h, json=body, timeout=120)
        if r.status_code >= 400:
            raise PublishError(f"postiz posts {r.status_code}: {r.text[:200]}")
        js = r.json()
        pid = str((js[0] if isinstance(js, list) and js else js).get("postId") or js)[:120]
        return PostResult(url=f"postiz://{pid}", platform_id=pid, extra={"integrations": integrations})
