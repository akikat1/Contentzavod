"""YouTube Data API v3: resumable-загрузка, обложка, субтитры.

С 01.06.2026 videos.insert — 1 unit в отдельном бакете (100 загрузок/сут на проект);
у канала есть свой скрытый лимит, который приходит как 403/429 uploadLimitExceeded —
такой отказ переносим на следующие сутки, а не считаем ошибкой.
"""
from __future__ import annotations

import json
import time

import httpx

from .base import Deferred, PostMeta, PostResult, Publisher, PublishError, Video

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"


def access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    r = httpx.post(TOKEN_URL, data={"client_id": client_id, "client_secret": client_secret,
                                    "refresh_token": refresh_token, "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        raise PublishError(f"youtube: не удалось обновить токен ({r.status_code}): {r.text[:200]} — "
                           f"перевыпустите refresh token (docs/SETUP.md)")
    return r.json()["access_token"]


class YouTubePublisher(Publisher):
    name = "youtube"

    def _token(self) -> str:
        return access_token(self.env("client_id_env"), self.env("client_secret_env"), self.env("refresh_token_env"))

    def publish(self, video: Video, meta: PostMeta) -> PostResult:
        tok = self._token()
        h = {"Authorization": f"Bearer {tok}"}
        is_short = video.height > video.width
        title = meta.title[:95] + (" #Shorts" if is_short and "#shorts" not in meta.title.lower() else "")
        desc = meta.description + ("\n\n" + " ".join(meta.hashtags) if meta.hashtags else "")
        body = {"snippet": {"title": title[:100], "description": desc[:4900], "tags": meta.tags[:15],
                            "categoryId": str(self.pc.get("category_id", "27")),
                            "defaultLanguage": self.cfg.language, "defaultAudioLanguage": self.cfg.language},
                "status": {"privacyStatus": self.pc.get("privacy", "private"), "selfDeclaredMadeForKids": False,
                           "containsSyntheticMedia": True}}
        size = video.path.stat().st_size
        init = httpx.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                          headers={**h, "Content-Type": "application/json; charset=UTF-8",
                                   "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
                          content=json.dumps(body), timeout=60)
        self._check(init)
        loc = init.headers["Location"]
        with open(video.path, "rb") as fh:       # потоково: мастер не грузим в память целиком
            up = httpx.put(loc, headers={**h, "Content-Type": "video/mp4", "Content-Length": str(size)},
                           content=iter(lambda: fh.read(1 << 22), b""), timeout=3600)
        self._check(up)
        vid = up.json()["id"]
        if video.thumbnail and video.thumbnail.exists() and not is_short:
            with open(video.thumbnail, "rb") as fh:
                t = httpx.post(f"{API.replace('/youtube/v3', '/upload/youtube/v3')}/thumbnails/set",
                               params={"videoId": vid}, headers={**h, "Content-Type": "image/jpeg"},
                               content=fh.read(), timeout=120)
            if t.status_code >= 400:
                pass       # обложка — не повод откатывать загрузку (у неподтверждённых каналов запрещена)
        if self.pc.get("upload_captions", True) and video.captions and video.captions.exists() and not is_short:
            meta_part = json.dumps({"snippet": {"videoId": vid, "language": self.cfg.language, "name": "",
                                                "isDraft": False}})
            files = {"metadata": (None, meta_part, "application/json"),
                     "media": ("captions.srt", video.captions.read_bytes(), "application/octet-stream")}
            httpx.post(f"{API.replace('/youtube/v3', '/upload/youtube/v3')}/captions",
                       params={"part": "snippet", "uploadType": "multipart"}, headers=h, files=files, timeout=120)
        url = f"https://youtube.com/shorts/{vid}" if is_short else f"https://youtu.be/{vid}"
        return PostResult(url=url, platform_id=vid)

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        txt = r.text
        if r.status_code in (403, 429) and any(k in txt for k in ("uploadLimitExceeded", "quotaExceeded",
                                                                   "rateLimitExceeded", "dailyLimitExceeded")):
            raise Deferred(f"youtube: лимит загрузок ({r.status_code})", time.time() + 24 * 3600)
        if r.status_code >= 500:
            raise Deferred(f"youtube: {r.status_code}", time.time() + 900)
        raise PublishError(f"youtube {r.status_code}: {txt[:300]}")
