"""TikTok Content Posting API (Direct Post). До аудита приложения посты принудительно
приватные (SELF_ONLY); после аудита — privacy из конфига. 6 запросов/мин на токен."""
from __future__ import annotations

import math
import time

import httpx

from .base import Deferred, PostMeta, PostResult, Publisher, PublishError, Video

API = "https://open.tiktokapis.com/v2"
CHUNK = 10 * 2**20


class TikTokPublisher(Publisher):
    name = "tiktok"

    def publish(self, video: Video, meta: PostMeta) -> PostResult:
        h = {"Authorization": f"Bearer {self.env('access_token_env')}", "Content-Type": "application/json; charset=UTF-8"}
        info = httpx.post(f"{API}/post/publish/creator_info/query/", headers=h, timeout=30)
        self._check(info)
        allowed = info.json()["data"].get("privacy_level_options", ["SELF_ONLY"])
        privacy = self.pc.get("privacy", "SELF_ONLY")
        if privacy not in allowed:
            privacy = "SELF_ONLY" if "SELF_ONLY" in allowed else allowed[0]
        size = video.path.stat().st_size
        chunk = size if size <= CHUNK else CHUNK
        total = max(1, math.floor(size / chunk)) if size > CHUNK else 1
        title = (meta.title + " " + " ".join(meta.hashtags))[:2200]
        init = httpx.post(f"{API}/post/publish/video/init/", headers=h, timeout=60, json={
            "post_info": {"title": title, "privacy_level": privacy, "disable_comment": False,
                          "disable_duet": False, "disable_stitch": False, "is_aigc": True},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                            "total_chunk_count": total}})
        self._check(init)
        data = init.json()["data"]
        with open(video.path, "rb") as fh:
            for i in range(total):
                start = i * chunk
                end = size - 1 if i == total - 1 else start + chunk - 1
                fh.seek(start)
                body = fh.read(end - start + 1)
                r = httpx.put(data["upload_url"], content=body, timeout=600, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{end}/{size}"})
                if r.status_code >= 400:
                    raise PublishError(f"tiktok upload chunk {i}: {r.status_code} {r.text[:200]}")
        pid = data["publish_id"]
        for _ in range(60):
            st = httpx.post(f"{API}/post/publish/status/fetch/", headers=h, json={"publish_id": pid}, timeout=30)
            self._check(st)
            status = st.json()["data"].get("status")
            if status == "PUBLISH_COMPLETE":
                ids = st.json()["data"].get("publicaly_available_post_id") or []
                url = f"https://www.tiktok.com/video/{ids[0]}" if ids else f"tiktok://publish/{pid}"
                return PostResult(url=url, platform_id=pid, extra={"privacy": privacy})
            if status == "FAILED":
                raise PublishError(f"tiktok: публикация отклонена: {st.json()['data'].get('fail_reason')}")
            time.sleep(10)
        raise Deferred("tiktok: публикация ещё обрабатывается", time.time() + 1800)

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code == 429:
            raise Deferred("tiktok: 6 запросов/мин на токен", time.time() + 90)
        err = (r.json().get("error") or {}) if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400 or (err.get("code") not in (None, "ok")):
            if err.get("code") == "spam_risk_too_many_posts":
                raise Deferred("tiktok: суточный лимит постов", time.time() + 24 * 3600)
            raise PublishError(f"tiktok {r.status_code}: {err or r.text[:200]}")
