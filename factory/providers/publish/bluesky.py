"""Bluesky: загрузка через video.bsky.app (app.bsky.video.uploadVideo) + пост с embed video.
Лимиты: до 10 мин и 300 МБ на ролик, суточный лимит видео на аккаунт."""
from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx

from .base import Deferred, PostMeta, PostResult, Publisher, PublishError, Video

PDS = "https://bsky.social"
VIDEO = "https://video.bsky.app"


class BlueskyPublisher(Publisher):
    name = "bluesky"

    def publish(self, video: Video, meta: PostMeta) -> PostResult:
        if video.duration > float(self.pc.get("max_video_s", 600)) or \
                video.path.stat().st_size > float(self.pc.get("max_video_mb", 300)) * 2**20:
            raise PublishError("bluesky: ролик длиннее 10 мин или больше 300 МБ — публикуйте шортс")
        s = httpx.post(f"{PDS}/xrpc/com.atproto.server.createSession", timeout=30,
                       json={"identifier": self.env("handle_env"), "password": self.env("app_password_env")})
        if s.status_code != 200:
            raise PublishError(f"bluesky: вход не удался: {s.text[:200]}")
        sess = s.json()
        jwt, did = sess["accessJwt"], sess["did"]
        pds_host = PDS
        for svc in (sess.get("didDoc") or {}).get("service", []):
            if svc.get("id") == "#atproto_pds":
                pds_host = svc["serviceEndpoint"]
        aud = "did:web:" + pds_host.split("://", 1)[-1]
        sa = httpx.get(f"{pds_host}/xrpc/com.atproto.server.getServiceAuth", timeout=30,
                       headers={"Authorization": f"Bearer {jwt}"},
                       params={"aud": aud, "lxm": "com.atproto.repo.uploadBlob", "exp": int(time.time()) + 1800})
        if sa.status_code != 200:
            raise PublishError(f"bluesky: service auth: {sa.text[:200]}")
        up = httpx.post(f"{VIDEO}/xrpc/app.bsky.video.uploadVideo", params={"did": did, "name": video.path.name},
                        headers={"Authorization": f"Bearer {sa.json()['token']}", "Content-Type": "video/mp4"},
                        content=video.path.read_bytes(), timeout=1800)
        if up.status_code == 429:
            raise Deferred("bluesky: суточный лимит видео", time.time() + 24 * 3600)
        if up.status_code >= 400 and "already_exists" not in up.text:
            raise PublishError(f"bluesky upload {up.status_code}: {up.text[:200]}")
        job = up.json().get("jobId") or (up.json().get("jobStatus") or {}).get("jobId")
        blob = (up.json().get("jobStatus") or {}).get("blob")
        for _ in range(180):
            if blob:
                break
            st = httpx.get(f"{VIDEO}/xrpc/app.bsky.video.getJobStatus", params={"jobId": job}, timeout=30,
                           headers={"Authorization": f"Bearer {jwt}"}).json().get("jobStatus", {})
            if st.get("state") == "JOB_STATE_FAILED":
                raise PublishError(f"bluesky: обработка видео не удалась: {st.get('error')}")
            blob = st.get("blob")
            time.sleep(5)
        if not blob:
            raise Deferred("bluesky: видео ещё обрабатывается", time.time() + 900)
        text = meta.title
        if len(text) + 2 + len(meta.description) <= 290:
            text += "\n\n" + meta.description
        record = {"$type": "app.bsky.feed.post", "text": text[:299], "langs": [self.cfg.language],
                  "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                  "embed": {"$type": "app.bsky.embed.video", "video": blob,
                            "aspectRatio": {"width": video.width, "height": video.height}}}
        cr = httpx.post(f"{pds_host}/xrpc/com.atproto.repo.createRecord", timeout=60,
                        headers={"Authorization": f"Bearer {jwt}"},
                        json={"repo": did, "collection": "app.bsky.feed.post", "record": record})
        if cr.status_code >= 400:
            raise PublishError(f"bluesky post {cr.status_code}: {cr.text[:200]}")
        uri = cr.json()["uri"]
        return PostResult(url=f"https://bsky.app/profile/{did}/post/{uri.rsplit('/', 1)[-1]}", platform_id=uri)
