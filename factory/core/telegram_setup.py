"""Помощники Telegram: найти канал и личный чат по getUpdates, переключить бота на локальный Bot API."""
from __future__ import annotations

import httpx

from .config import Config
from .verify import telegram_api


def parse_updates(updates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Каналы/группы, где бот администратор, и личные чаты, где написали /start."""
    channels: dict[int, dict] = {}
    privates: dict[int, dict] = {}
    for u in updates:
        mcm = u.get("my_chat_member")
        if mcm:
            chat = mcm.get("chat", {})
            status = (mcm.get("new_chat_member") or {}).get("status")
            if chat.get("type") in ("channel", "supergroup", "group"):
                if status == "administrator":
                    channels[chat["id"]] = {"id": chat["id"], "title": chat.get("title", ""),
                                            "username": chat.get("username")}
                elif status in ("left", "kicked", "member"):
                    channels.pop(chat["id"], None)
        post = u.get("channel_post")
        if post:
            chat = post.get("chat", {})
            channels.setdefault(chat["id"], {"id": chat["id"], "title": chat.get("title", ""),
                                             "username": chat.get("username")})
        msg = u.get("message")
        if msg and (msg.get("chat") or {}).get("type") == "private" and (msg.get("text") or "").startswith("/start"):
            chat = msg["chat"]
            privates[chat["id"]] = {"id": chat["id"], "name": " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username", "")}
    return list(channels.values()), list(privates.values())


def fetch_updates(cfg: Config, token: str) -> tuple[list[dict], str]:
    """getUpdates: сначала локальный сервер (если бот уже переключён), иначе облако."""
    last_err = ""
    for local in (True, False):
        try:
            r = httpx.get(f"{telegram_api(cfg, local)}/bot{token}/getUpdates",
                          params={"allowed_updates": '["message","channel_post","my_chat_member"]', "limit": 100},
                          timeout=15)
        except httpx.HTTPError as e:
            last_err = str(e)
            continue
        js = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if js.get("ok"):
            return js["result"], "локальный сервер" if local else "облако"
        last_err = js.get("description", f"HTTP {r.status_code}")
    raise RuntimeError(f"getUpdates не удался: {last_err}")


def switch_to_local(cfg: Config, token: str) -> str:
    """logOut из облачного API и проверка локального сервера. После logOut облако недоступно 10 минут."""
    local = telegram_api(cfg, True)
    try:
        me = httpx.get(f"{local}/bot{token}/getMe", timeout=10)
        if me.status_code == 200 and me.json().get("ok"):
            return "бот уже работает через локальный сервер"
    except httpx.HTTPError:
        raise RuntimeError("локальный Bot API не запущен — агент поднимет контейнер telegram-bot-api") from None
    out = httpx.post(f"https://api.telegram.org/bot{token}/logOut", timeout=15).json()
    if not out.get("ok") and "logged out" not in str(out.get("description", "")).lower():
        raise RuntimeError(f"logOut не удался: {out.get('description')}")
    for _ in range(10):
        try:
            me = httpx.get(f"{local}/bot{token}/getMe", timeout=15)
            if me.status_code == 200 and me.json().get("ok"):
                return f"бот @{me.json()['result']['username']} переключён на локальный сервер"
        except httpx.HTTPError:
            pass
    raise RuntimeError("после logOut локальный сервер не авторизовал бота — проверьте TELEGRAM_API_ID/HASH")
