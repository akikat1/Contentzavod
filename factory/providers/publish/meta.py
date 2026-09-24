"""Instagram Reels и Facebook Reels через Graph API (после Meta app review).
Обе площадки забирают видео по публичной ссылке — нужен буфер R2."""
from __future__ import annotations

import time

import httpx

from .base import Deferred, PostMeta, PostResult, Publisher, PublishError, Video

GRAPH = "https://graph.facebook.com"


def _check(r: httpx.Response, name: str) -> dict:
    js = r.json() if r.headers.get("content-type", "").startswith(("application/json", "text/javascript")) else {}
    err = js.get("error") or {}
    if r.status_code >= 400 or err:
        if err.get("code") in (4, 17, 32, 613) or r.status_code == 429:
            raise Deferred(f"{name}: лимит Graph API ({err.get('message')})", time.time() + 3600)
        raise PublishError(f"{name} {r.status_code}: {err.get('message') or r.text[:200]}")
    return js


class InstagramPublisher(Publisher):
    name = "instagram"
    needs_public_url = True

    def publish(self, video: Video, meta: PostMeta) -> PostResult:
        if not video.public_url:
            raise PublishError("instagram: нужен публичный URL (publish.remote_storage)")
        if not (5 <= video.duration <= 90) or video.height <= video.width:
            raise PublishError("instagram reels: нужно 9:16 и 5–90 с")
        v = self.pc.get("graph_version", "v21.0")
        user, tok = self.env("ig_user_id_env"), self.env("access_token_env")
        caption = (meta.description + "\n\n" + " ".join(meta.hashtags)).strip()[:2200]
        c = _check(httpx.post(f"{GRAPH}/{v}/{user}/media", timeout=60, data={
            "media_type": "REELS", "video_url": video.public_url, "caption": caption, "share_to_feed": "true",
            "access_token": tok}), self.name)
        cid = c["id"]
        for _ in range(60):
            st = _check(httpx.get(f"{GRAPH}/{v}/{cid}", params={"fields": "status_code,status", "access_token": tok},
                                  timeout=30), self.name)
            if st.get("status_code") == "FINISHED":
                break
            if st.get("status_code") == "ERROR":
                raise PublishError(f"instagram: контейнер не обработан: {st.get('status')}")
            time.sleep(10)
        else:
            raise Deferred("instagram: обработка затянулась", time.time() + 1800)
        pub = _check(httpx.post(f"{GRAPH}/{v}/{user}/media_publish", data={"creation_id": cid, "access_token": tok},
                                timeout=60), self.name)
        mid = pub["id"]
        link = _check(httpx.get(f"{GRAPH}/{v}/{mid}", params={"fields": "permalink", "access_token": tok}, timeout=30),
                      self.name).get("permalink", f"https://instagram.com/reel/{mid}")
        return PostResult(url=link, platform_id=mid)


class FacebookPublisher(Publisher):
    name = "facebook"
    needs_public_url = True

    def publish(self, video: Video, meta: PostMeta) -> PostResult:
        if not video.public_url:
            raise PublishError("facebook: нужен публичный URL (publish.remote_storage)")
        v = self.pc.get("graph_version", "v21.0")
        page, tok = self.env("page_id_env"), self.env("access_token_env")
        start = _check(httpx.post(f"{GRAPH}/{v}/{page}/video_reels", data={"upload_phase": "start",
                                                                             "access_token": tok}, timeout=60), self.name)
        vid, upload_url = start["video_id"], start["upload_url"]
        _check(httpx.post(upload_url, headers={"Authorization": f"OAuth {tok}", "file_url": video.public_url},
                          timeout=600), self.name)
        _check(httpx.post(f"{GRAPH}/{v}/{page}/video_reels", timeout=60, data={
            "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED", "access_token": tok,
            "description": (meta.description + "\n\n" + " ".join(meta.hashtags)).strip()[:2200]}), self.name)
        return PostResult(url=f"https://www.facebook.com/reel/{vid}", platform_id=vid)
