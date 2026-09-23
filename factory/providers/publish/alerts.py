"""Алерты оператору в Telegram (карантин, мёртвые ключи, упавшие публикации)."""
from __future__ import annotations

import os

import httpx

from ...core.config import Config


def send_alert(cfg: Config, text: str) -> bool:
    a = cfg.section("alerts.telegram")
    if not a.get("enabled"):
        return False
    token = os.environ.get(a.get("token_env", "TG_BOT_TOKEN"), "")
    chat = os.environ.get(a.get("chat_id_env", "TG_ADMIN_CHAT_ID"), "")
    if not (token and chat):
        return False
    r = httpx.post(f"{a.get('api_url', 'https://api.telegram.org')}/bot{token}/sendMessage",
                   data={"chat_id": chat, "text": f"🏭 {cfg.get('channel.name', 'Контент-завод')}: {text}"[:4000]},
                   timeout=30)
    return r.status_code == 200
