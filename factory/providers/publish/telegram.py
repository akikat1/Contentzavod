"""Telegram: sendVideo через ЛОКАЛЬНЫЙ Bot API server (до 2 ГБ). Облачный Bot API режет файлы
на 50 МБ — 5–10-минутный ролик туда не пролезет, поэтому по умолчанию локальный сервер."""
from __future__ import annotations

import time

import httpx

from .base import Deferred, PostMeta, PostResult, Publisher, PublishError, Video


class TelegramPublisher(Publisher):
    name = "telegram"

    def publish(self, video: Video, meta: PostMeta) -> PostResult:
        token, chat = self.env("token_env"), self.env("chat_id_env")
        base = self.pc.get("bot_api_url", "http://127.0.0.1:8081").rstrip("/")
        limit_mb = 50 if "api.telegram.org" in base else 2000
        size_mb = video.path.stat().st_size / 2**20
        if size_mb > limit_mb:
            raise PublishError(f"telegram: файл {size_mb:.0f} МБ больше лимита {limit_mb} МБ для {base}")
        if limit_mb > 50:
            self._wait_local(base, token)
        caption = f"<b>{_esc(meta.title)}</b>\n\n{_esc(meta.description)}"
        if meta.hashtags:
            caption += "\n\n" + " ".join(meta.hashtags)
        files = {"video": (video.path.name, open(video.path, "rb"), "video/mp4")}  # noqa: SIM115
        if video.thumbnail and video.thumbnail.exists():
            files["thumbnail"] = (video.thumbnail.name, open(video.thumbnail, "rb"), "image/jpeg")  # noqa: SIM115
        data = {"chat_id": chat, "caption": caption[:1024], "parse_mode": "HTML", "supports_streaming": "true",
                "width": video.width, "height": video.height, "duration": int(video.duration)}
        if "thumbnail" in files:
            data["thumbnail"] = "attach://thumbnail"
        try:
            r = httpx.post(f"{base}/bot{token}/sendVideo", data=data, files=files, timeout=3600)
        except httpx.HTTPError as e:
            raise Deferred(f"telegram: сеть/локальный сервер недоступен: {e}", time.time() + 600) from e
        finally:
            for f in files.values():
                f[1].close()
        js = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 429:
            retry = (js.get("parameters") or {}).get("retry_after", 60)
            raise Deferred("telegram: flood limit", time.time() + retry)
        if not js.get("ok"):
            raise PublishError(f"telegram {r.status_code}: {js.get('description') or r.text[:200]}")
        msg = js["result"]
        chat_obj = msg.get("chat", {})
        uname = chat_obj.get("username")
        url = f"https://t.me/{uname}/{msg['message_id']}" if uname else f"tg://chat/{chat}/{msg['message_id']}"
        return PostResult(url=url, platform_id=str(msg["message_id"]))


    @staticmethod
    def _wait_local(base: str, token: str, timeout_s: float = 60) -> None:
        """Тик только что поднял WSL — контейнер Bot API может стартовать ещё несколько секунд."""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                if httpx.get(f"{base}/bot{token}/getMe", timeout=5).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(3)
        raise Deferred("telegram: локальный Bot API не отвечает", time.time() + 600)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
