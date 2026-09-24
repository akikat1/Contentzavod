"""Живые проверки: ключи LLM, модели провайдеров, секреты площадок.

Каждая функция возвращает (ok, сообщение) и не бросает исключений — результат
показывается в отчёте готовности и в мастере настройки.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from .config import Config
from .keypool import KeyPool


@dataclass
class ProbeResult:
    key_id: str
    provider: str
    model: str
    status: str          # ok | rate_limited | auth | bad_model | error | skipped
    message: str
    seconds: float = 0.0


def probe_keys(cfg: Config, pool: KeyPool, only: set[str] | None = None) -> list[ProbeResult]:
    """Реальный минимальный вызов каждым ключом; исход записывается в пул."""
    from ..providers.llm.base import AuthError, BadRequest, LLMError, Message, RateLimited  # noqa: PLC0415
    from ..providers.llm.router import LLMRouter  # noqa: PLC0415
    router = LLMRouter(cfg, pool)
    out: list[ProbeResult] = []
    for key in pool.keys.values():
        if only and key.key_id not in only:
            continue
        task = next((t for t in ("metadata", "judge", "script") if pool.model_for(key.provider, t)), None)
        if not task:
            out.append(ProbeResult(key.key_id, key.provider, "", "skipped", "нет модели для текста"))
            continue
        model = pool.model_for(key.provider, task)
        t0 = time.time()
        try:
            res = router._client(key.provider).complete(
                system="", messages=[Message("user", "Ответь одним словом: ок")], model=model,
                api_key=key.secret, json_mode=False, max_tokens=16, temperature=0)
            pool._report(key, "ok", tokens=res.tokens)
            out.append(ProbeResult(key.key_id, key.provider, model, "ok", "работает", round(time.time() - t0, 1)))
        except RateLimited as e:
            pool._report(key, "rate_limited", retry_after=e.retry_after, daily=e.daily, message=str(e))
            out.append(ProbeResult(key.key_id, key.provider, model, "rate_limited", str(e)[:160]))
        except AuthError as e:
            pool._report(key, "auth", message=str(e))
            out.append(ProbeResult(key.key_id, key.provider, model, "auth", str(e)[:160]))
        except BadRequest as e:
            out.append(ProbeResult(key.key_id, key.provider, model, "bad_model",
                                   f"модель {model} отвергнута: {str(e)[:120]}"))
        except (LLMError, httpx.HTTPError) as e:
            pool._report(key, "error", message=str(e))
            out.append(ProbeResult(key.key_id, key.provider, model, "error", str(e)[:160]))
    return out


def list_models(cfg: Config, pool: KeyPool) -> dict[str, list[str]]:
    """Актуальные модели каждого провайдера — чтобы чинить 404 «model not found» конфигом, а не кодом."""
    out: dict[str, list[str]] = {}
    for provider in cfg.section("llm.providers"):
        key = next((k for k in pool.keys.values() if k.provider == provider), None)
        if key is None:
            continue
        pc = cfg.section(f"llm.providers.{provider}")
        base = pc["base_url"].rstrip("/")
        try:
            if pc.get("kind") == "gemini":
                r = httpx.get(f"{base}/models", params={"key": key.secret, "pageSize": 200}, timeout=30)
                r.raise_for_status()
                names = []
                for m in r.json().get("models", []):
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" in methods or "embedContent" in methods:
                        names.append(m["name"].removeprefix("models/"))
            else:
                r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key.secret}"}, timeout=30)
                r.raise_for_status()
                names = [m["id"] for m in r.json().get("data", [])]
                if provider == "openrouter":
                    names = [n for n in names if n.endswith(":free")]
            out[provider] = sorted(names)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            out[provider] = [f"ошибка: {e}"]
    return out


# ------------------------------------------------------------------ площадки

def _env(name: str) -> str:
    return os.environ.get(name, "")


def check_section(cfg: Config, sid: str) -> tuple[bool, str]:
    try:
        fn = CHECKS.get(sid)
        return fn(cfg) if fn else (True, "формат проверен")
    except httpx.HTTPError as e:
        return False, f"сеть: {e}"
    except Exception as e:  # noqa: BLE001 — проверка не должна ронять мастер
        return False, f"{type(e).__name__}: {e}"


def _stock(cfg: Config) -> tuple[bool, str]:
    msgs, ok = [], True
    pk = _env(cfg.get("visual.pexels.key_env", "PEXELS_API_KEY"))
    if pk:
        r = httpx.get("https://api.pexels.com/videos/search", headers={"Authorization": pk},
                      params={"query": "ocean", "per_page": 1}, timeout=20)
        ok &= r.status_code == 200
        msgs.append(f"Pexels: {'работает' if r.status_code == 200 else f'ошибка {r.status_code}'}")
    xk = _env(cfg.get("visual.pixabay.key_env", "PIXABAY_API_KEY"))
    if xk:
        r = httpx.get("https://pixabay.com/api/videos/", params={"key": xk, "q": "ocean", "per_page": 3}, timeout=20)
        ok &= r.status_code == 200
        msgs.append(f"Pixabay: {'работает' if r.status_code == 200 else f'ошибка {r.status_code}'}")
    return (ok and bool(msgs)), "; ".join(msgs) or "ключей нет"


def _youtube(cfg: Config) -> tuple[bool, str]:
    from ..providers.publish.youtube import access_token  # noqa: PLC0415
    p = "publish.platforms.youtube"
    cid, sec, ref = (_env(cfg.get(f"{p}.{k}", d)) for k, d in (("client_id_env", "YT_CLIENT_ID"),
                                                                ("client_secret_env", "YT_CLIENT_SECRET"),
                                                                ("refresh_token_env", "YT_REFRESH_TOKEN")))
    if not (cid and sec):
        return False, "нет Client ID/Secret"
    if not ref:
        return False, "нажмите «Авторизовать YouTube»"
    tok = access_token(cid, sec, ref)
    r = httpx.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                  headers={"Authorization": f"Bearer {tok}"}, timeout=20)
    items = r.json().get("items", []) if r.status_code == 200 else []
    if not items:
        return False, f"авторизация есть, но канала нет ({r.status_code})"
    return True, f"канал «{items[0]['snippet']['title']}»"


def telegram_api(cfg: Config, local: bool) -> str:
    return (cfg.get("publish.platforms.telegram.bot_api_url", "http://127.0.0.1:8081") if local
            else "https://api.telegram.org").rstrip("/")


def _telegram(cfg: Config) -> tuple[bool, str]:
    tok = _env(cfg.get("publish.platforms.telegram.token_env", "TG_BOT_TOKEN"))
    if not tok:
        return False, "нет токена бота"
    for local in (True, False):
        try:
            r = httpx.get(f"{telegram_api(cfg, local)}/bot{tok}/getMe", timeout=10)
        except httpx.HTTPError:
            continue
        if r.status_code == 200 and r.json().get("ok"):
            where = "локальный сервер" if local else "облачный API (агент переключит на локальный)"
            chan = _env(cfg.get("publish.platforms.telegram.chat_id_env", "TG_CHANNEL_ID"))
            return bool(chan), f"бот @{r.json()['result']['username']} через {where}" + \
                ("" if chan else "; канал не найден — нажмите «Найти канал»")
    return False, "бот не отвечает ни через локальный сервер, ни через облако — проверьте токен"


def _bluesky(cfg: Config) -> tuple[bool, str]:
    h = _env(cfg.get("publish.platforms.bluesky.handle_env", "BSKY_HANDLE"))
    pw = _env(cfg.get("publish.platforms.bluesky.app_password_env", "BSKY_APP_PASSWORD"))
    if not (h and pw):
        return False, "нет handle или app password"
    r = httpx.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                   json={"identifier": h, "password": pw}, timeout=20)
    if r.status_code != 200:
        return False, f"вход не удался: {r.json().get('message', r.status_code)}"
    js = r.json()
    if js.get("emailConfirmed") is False:
        return False, "почта аккаунта не подтверждена — Bluesky не примет видео"
    return True, f"вход как {js.get('handle')}"


def _vk(cfg: Config) -> tuple[bool, str]:
    tok = _env(cfg.get("publish.platforms.vk.token_env", "VK_TOKEN"))
    gid = _env(cfg.get("publish.platforms.vk.group_id_env", "VK_GROUP_ID")).lstrip("-")
    if not tok:
        return False, "нет токена — «Войти через VK»"
    r = httpx.post("https://api.vk.com/method/groups.getById", timeout=20,
                   data={"access_token": tok, "v": cfg.get("publish.platforms.vk.api_version", "5.199"),
                         "group_id": gid or "1"})
    js = r.json()
    if "error" in js:
        return False, f"VK: {js['error'].get('error_msg')}"
    if not gid:
        return False, "токен рабочий, укажите ID сообщества"
    groups = (js.get("response") or {}).get("groups") or js.get("response") or []
    name = groups[0].get("name") if groups else gid
    return True, f"сообщество «{name}»"


def _tiktok(cfg: Config) -> tuple[bool, str]:
    tok = _env(cfg.get("publish.platforms.tiktok.access_token_env", "TIKTOK_ACCESS_TOKEN"))
    if not tok:
        return False, "нет токена (ждёт одобрения заявки)"
    r = httpx.post("https://open.tiktokapis.com/v2/post/publish/creator_info/query/", timeout=20,
                   headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8"})
    ok = r.status_code == 200 and (r.json().get("error") or {}).get("code") in (None, "ok")
    return ok, "токен рабочий" if ok else f"ошибка {r.status_code}: {r.text[:120]}"


def _instagram(cfg: Config) -> tuple[bool, str]:
    uid = _env(cfg.get("publish.platforms.instagram.ig_user_id_env", "IG_USER_ID"))
    tok = _env(cfg.get("publish.platforms.instagram.access_token_env", "META_ACCESS_TOKEN"))
    if not (uid and tok):
        return False, "нет IG User ID или токена"
    v = cfg.get("publish.platforms.instagram.graph_version", "v21.0")
    r = httpx.get(f"https://graph.facebook.com/{v}/{uid}", params={"fields": "username", "access_token": tok},
                  timeout=20)
    return (r.status_code == 200, f"аккаунт @{r.json().get('username')}" if r.status_code == 200
            else f"ошибка: {r.text[:120]}")


def _postiz(cfg: Config) -> tuple[bool, str]:
    key = _env(cfg.get("publish.platforms.postiz.api_key_env", "POSTIZ_API_KEY"))
    base = cfg.get("publish.platforms.postiz.base_url", "http://127.0.0.1:5000/public/v1").rstrip("/")
    if not key:
        return False, "нет API-ключа"
    r = httpx.get(f"{base}/integrations", headers={"Authorization": key}, timeout=15)
    if r.status_code != 200:
        return False, f"Postiz ответил {r.status_code}"
    items = r.json() if isinstance(r.json(), list) else r.json().get("integrations", [])
    return True, f"подключено каналов: {len(items)}"


CHECKS = {"stock": _stock, "youtube": _youtube, "telegram": _telegram, "bluesky": _bluesky, "vk": _vk,
          "tiktok": _tiktok, "instagram": _instagram, "postiz": _postiz}
