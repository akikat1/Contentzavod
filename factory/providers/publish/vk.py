"""VK Видео: video.save → загрузка файла на выданный upload_url → ролик в сообществе."""
from __future__ import annotations

import time

import httpx

from .base import Deferred, PostMeta, PostResult, Publisher, PublishError, Video


class VKPublisher(Publisher):
    name = "vk"

    def publish(self, video: Video, meta: PostMeta) -> PostResult:
        token, group = self.env("token_env"), self.env("group_id_env")
        v = str(self.pc.get("api_version", "5.199"))
        r = httpx.post("https://api.vk.com/method/video.save", timeout=60, data={
            "access_token": token, "v": v, "group_id": group.lstrip("-"), "name": meta.title[:128],
            "description": (meta.description + ("\n\n" + " ".join(meta.hashtags) if meta.hashtags else ""))[:5000],
            "wallpost": 1, "repeat": 0})
        js = r.json()
        if "error" in js:
            err = js["error"]
            if err.get("error_code") in (6, 9, 29):
                raise Deferred(f"vk: лимит ({err.get('error_msg')})", time.time() + 3600)
            raise PublishError(f"vk video.save: {err.get('error_code')} {err.get('error_msg')}")
        upload_url = js["response"]["upload_url"]
        with open(video.path, "rb") as fh:
            up = httpx.post(upload_url, files={"video_file": (video.path.name, fh, "video/mp4")}, timeout=3600)
        if up.status_code >= 400:
            raise PublishError(f"vk upload {up.status_code}: {up.text[:200]}")
        res = up.json()
        owner = res.get("owner_id", js["response"].get("owner_id"))
        vid = res.get("video_id", js["response"].get("video_id"))
        return PostResult(url=f"https://vk.com/video{owner}_{vid}", platform_id=f"{owner}_{vid}")
