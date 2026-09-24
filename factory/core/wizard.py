"""Мастер настройки — локальная веб-страница для шагов, которым нужен человек.

Всё, что требует аккаунтов (ключи, OAuth, токены), вводится здесь и сразу пишется
в .env / config/keys.yaml на этом компьютере. Секреты не проходят через чат с агентом
и на странице показываются только маской. Сервер слушает только 127.0.0.1; каждая
форма несёт случайный токен (защита от чужих страниц в браузере), заголовок Host
проверяется (защита от DNS rebinding).
"""
from __future__ import annotations

import html
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import auth
from .config import Config, set_local
from .db import DB
from .envfile import env_path, mask, read_env, set_env
from .keypool import KeyPool, append_keys_file
from .requirements import Section, sections
from .setup_check import run_checks, summary, to_json
from .telegram_setup import fetch_updates, parse_updates, switch_to_local
from .verify import check_section, probe_keys

log = logging.getLogger("factory.wizard")

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2230;--mute:#5d6678;--ok:#1f8a4c;--bad:#c23b3b;--warn:#b7791f;--acc:#2f5bd3;
--line:#e3e6ec}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b1f27;--ink:#e8ebf1;--mute:#9aa3b5;--line:#2b313d;
--acc:#7fa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}
.sub{color:var(--mute);margin:0 0 20px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin:14px 0}.card h2{font-size:17px;margin:0 0 8px;display:flex;gap:10px;align-items:center}
.badge{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid currentColor}.ok{color:var(--ok)}
.bad{color:var(--bad)}.warn{color:var(--warn)}.mute{color:var(--mute)}ol{margin:6px 0 10px 20px;padding:0}
label{display:block;margin:8px 0 2px;font-weight:600;font-size:13px}input,select,textarea{width:100%;padding:8px;
border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
textarea{min-height:90px}button{margin-top:10px;padding:8px 14px;border:0;border-radius:6px;background:var(--acc);
color:#fff;font:inherit;cursor:pointer}button.ghost{background:transparent;color:var(--acc);border:1px solid var(--acc)}
.row{display:flex;gap:8px;flex-wrap:wrap}.msg{margin-top:10px;padding:8px 10px;border-radius:6px;background:var(--bg)}
.links a{margin-right:12px}details summary{cursor:pointer;color:var(--mute)}.hint{color:var(--mute);font-size:13px}
.check{display:flex;gap:8px;align-items:center;margin-top:8px}.check input{width:auto}
"""


class Wizard:
    def __init__(self, cfg: Config, db: DB, port: int):
        self.cfg, self.db, self.port = cfg, db, port
        self.csrf = secrets.token_urlsafe(24)
        self.messages: dict[str, tuple[bool, str]] = {}
        self.oauth_state: str | None = None
        self.tg_found: tuple[list[dict], list[dict]] | None = None
        self.env_file = env_path(cfg.root)

    @property
    def redirect(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def env(self) -> dict[str, str]:
        return read_env(self.env_file)

    def value(self, name: str) -> str:
        return os.environ.get(name) or self.env().get(name, "")

    # ------------------------------------------------------------------ действия
    def save(self, sid: str, form: dict[str, str]) -> None:
        sec = next(s for s in sections() if s.id == sid)
        allowed = {v.name(self.cfg) for v in sec.vars if not v.filled_by or v.filled_by == "vk_url"}
        changed = set_env(self.env_file, {k: v for k, v in form.items() if k in allowed})
        if sec.platform and "enable" in form:
            set_local(self.cfg, f"publish.platforms.{sec.platform}.enabled", form["enable"] == "1")
        ok, msg = check_section(self.cfg, sid)
        self.messages[sid] = (ok, (f"Сохранено: {', '.join(changed)}. " if changed else "") + msg)

    def import_keys(self, provider: str, text: str) -> None:
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        n = append_keys_file(self.cfg.path("llm.keys_file", "config/keys.yaml"), provider, lines)
        pool = KeyPool.from_config(self.db, self.cfg)
        ids = {k.key_id for k in pool.keys.values() if k.provider == provider}
        res = probe_keys(self.cfg, pool, only=ids)
        alive = sum(1 for r in res if r.status == "ok")
        bad_model = any(r.status == "bad_model" for r in res)
        self.messages["llm"] = (alive > 0, f"{provider}: добавлено {n}, работает {alive} из {len(res)}"
                                + (". Модель отвергнута — агент поправит имя модели" if bad_model else ""))

    def oauth_start(self) -> str:
        cid = self.value(self.cfg.get("publish.platforms.youtube.client_id_env", "YT_CLIENT_ID"))
        if not cid:
            raise RuntimeError("сначала сохраните Client ID и Client Secret")
        self.oauth_state = secrets.token_urlsafe(16)
        return auth.youtube_auth_url(cid, self.redirect, self.oauth_state)

    def oauth_finish(self, q: dict[str, str]) -> None:
        if q.get("state") != self.oauth_state:
            self.messages["youtube"] = (False, "ответ Google не от этого мастера — нажмите «Авторизовать» ещё раз")
            return
        self.oauth_state = None
        if "error" in q:
            self.messages["youtube"] = (False, f"Google: {q['error']}")
            return
        p = "publish.platforms.youtube"
        tok = auth.youtube_exchange(q["code"], self.value(self.cfg.get(f"{p}.client_id_env", "YT_CLIENT_ID")),
                                    self.value(self.cfg.get(f"{p}.client_secret_env", "YT_CLIENT_SECRET")),
                                    self.redirect)
        set_env(self.env_file, {self.cfg.get(f"{p}.refresh_token_env", "YT_REFRESH_TOKEN"): tok})
        ok, msg = check_section(self.cfg, "youtube")
        self.messages["youtube"] = (ok, "Авторизация сохранена. " + msg)

    def tg_discover(self) -> None:
        token = self.value(self.cfg.get("publish.platforms.telegram.token_env", "TG_BOT_TOKEN"))
        if not token:
            raise RuntimeError("сначала сохраните токен бота")
        updates, where = fetch_updates(self.cfg, token)
        channels, privates = parse_updates(updates)
        self.tg_found = (channels, privates)
        updates_env = {}
        if len(channels) == 1:
            updates_env[self.cfg.get("publish.platforms.telegram.chat_id_env", "TG_CHANNEL_ID")] = str(channels[0]["id"])
        if len(privates) == 1:
            updates_env[self.cfg.get("alerts.telegram.chat_id_env", "TG_ADMIN_CHAT_ID")] = str(privates[0]["id"])
        set_env(self.env_file, updates_env)
        if updates_env and len(privates) == 1:
            set_local(self.cfg, "alerts.telegram.enabled", True)
        found = (f"каналов: {len(channels)} ({', '.join(c['title'] for c in channels) or '—'}); "
                 f"личных чатов с /start: {len(privates)}")
        auto = "Сохранено автоматически. " if updates_env else ""
        need = []
        if not channels:
            need.append("добавьте бота админом канала и напишите в канал сообщение")
        if not privates:
            need.append("напишите боту /start в личку")
        if len(channels) > 1 or len(privates) > 1:
            need.append("выберите нужные ниже")
        self.messages["telegram"] = (bool(channels), f"Через {where}: {found}. {auto}" + "; ".join(need))

    def tg_choose(self, form: dict[str, str]) -> None:
        upd = {}
        if form.get("channel"):
            upd[self.cfg.get("publish.platforms.telegram.chat_id_env", "TG_CHANNEL_ID")] = form["channel"]
        if form.get("private"):
            upd[self.cfg.get("alerts.telegram.chat_id_env", "TG_ADMIN_CHAT_ID")] = form["private"]
            set_local(self.cfg, "alerts.telegram.enabled", True)
        set_env(self.env_file, upd)
        self.messages["telegram"] = (True, "Выбор сохранён")

    def tg_local(self) -> None:
        token = self.value(self.cfg.get("publish.platforms.telegram.token_env", "TG_BOT_TOKEN"))
        self.messages["telegram"] = (True, switch_to_local(self.cfg, token))

    def vk_token(self, url: str) -> None:
        frag = urllib.parse.urlparse(url.strip()).fragment or urllib.parse.urlparse(url.strip()).query
        q = dict(urllib.parse.parse_qsl(frag))
        token = q.get("access_token") or (url.strip() if url.strip().startswith("vk1.") else "")
        if not token:
            self.messages["vk"] = (False, "в адресе нет access_token — скопируйте адрес целиком после входа")
            return
        set_env(self.env_file, {self.cfg.get("publish.platforms.vk.token_env", "VK_TOKEN"): token})
        ok, msg = check_section(self.cfg, "vk")
        self.messages["vk"] = (ok, "Токен сохранён. " + msg)

    # ------------------------------------------------------------------ страница
    def vk_auth_url(self) -> str:
        app = self.value("VK_APP_ID")
        if not app:
            return ""
        return "https://oauth.vk.com/authorize?" + urllib.parse.urlencode({
            "client_id": app, "display": "page", "redirect_uri": "https://oauth.vk.com/blank.html",
            "scope": "video,wall,groups,offline", "response_type": "token",
            "v": self.cfg.get("publish.platforms.vk.api_version", "5.199")})

    def page(self) -> str:
        items = run_checks(self.cfg, self.db)
        s = summary(items)
        human = [i for i in items if i.status == "fail" and i.category == "human"]
        head = (f"<div class='card'><h2>Осталось сделать вам: {len(human)}"
                f"<span class='badge {'ok' if not human else 'warn'}'>"
                f"{'всё готово' if not human else 'по порядку сверху вниз'}</span></h2>"
                + ("<ol>" + "".join(f"<li>{html.escape(i.title)}</li>" for i in human) + "</ol>" if human else
                   "<p>Ваша часть настройки закончена — возвращайтесь к агенту.</p>")
                + f"<p class='hint'>Агенту осталось автоматических пунктов: {len(s['auto_todo'])}. "
                  f"Страница обновляется после каждого сохранения.</p></div>")
        body = [head]
        env = self.env()
        pool_counts: dict[str, int] = {}
        for k in KeyPool.from_config(self.db, self.cfg).keys.values():
            pool_counts[k.provider] = pool_counts.get(k.provider, 0) + 1
        for sec in sections():
            body.append(self.section_html(sec, env, pool_counts))
        return (f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' "
                f"content='width=device-width,initial-scale=1'><title>Контент-завод: настройка</title>"
                f"<style>{CSS}</style></head><body><main><h1>Контент-завод: настройка</h1>"
                f"<p class='sub'>Всё, что вы вводите, сохраняется только на этом компьютере (.env, config/keys.yaml) "
                f"и показывается маской.</p>{''.join(body)}</main></body></html>")

    def _form(self, action: str, inner: str, button: str, ghost: bool = False) -> str:
        return (f"<form method='post' action='{action}'><input type='hidden' name='csrf' value='{self.csrf}'>"
                f"{inner}<button class='{'ghost' if ghost else ''}'>{html.escape(button)}</button></form>")

    def section_html(self, sec: Section, env: dict[str, str], pool_counts: dict[str, int]) -> str:
        cfg = self.cfg
        if sec.id == "llm":
            done = sum(pool_counts.values()) > 0
            status = ", ".join(f"{p}: {n}" for p, n in sorted(pool_counts.items())) or "ключей нет"
        else:
            miss = [v for v in sec.vars if not v.optional and not (os.environ.get(v.name(cfg)) or env.get(v.name(cfg)))]
            done = not miss
            status = "заполнено" if done else f"не хватает: {', '.join(v.label for v in miss)}"
        badge = f"<span class='badge {'ok' if done else 'bad'}'>{html.escape(status)}</span>"
        steps = "<ol>" + "".join(f"<li>{html.escape(x)}</li>" for x in sec.steps) + "</ol>"
        links = "<p class='links'>" + "".join(
            f"<a href='{html.escape(u)}' target='_blank' rel='noopener'>{html.escape(t)} ↗</a>" for t, u in sec.links) + "</p>"
        msg = ""
        if sec.id in self.messages:
            ok, text = self.messages[sec.id]
            msg = f"<div class='msg {'ok' if ok else 'bad'}'>{html.escape(text)}</div>"
        parts = [steps, links]
        if sec.id == "llm":
            parts.append(self._form("/keys", "<label>Провайдер</label><select name='provider'>"
                                    "<option>gemini</option><option>groq</option><option>openrouter</option></select>"
                                    "<label>Ключи — по одному в строке</label><textarea name='keys' "
                                    "autocomplete='off' spellcheck='false'></textarea>", "Добавить и проверить"))
        else:
            fields = []
            for v in sec.vars:
                name = v.name(cfg)
                cur = os.environ.get(name) or env.get(name, "")
                if v.filled_by and v.filled_by != "vk_url":
                    fields.append(f"<p class='hint'>{html.escape(v.label)}: "
                                  f"{html.escape(cur if not v.secret else mask(cur)) if cur else 'заполнится автоматически'}</p>")
                    continue
                if v.filled_by == "vk_url":
                    continue
                ph = (cur if not v.secret else mask(cur)) if cur else ""
                typ = "password" if v.secret else "text"
                fields.append(f"<label>{html.escape(v.label)} <span class='mute'>({name})</span></label>"
                              f"<input type='{typ}' name='{name}' placeholder='{html.escape(ph)}' autocomplete='off'>")
            if sec.platform:
                on = bool(cfg.get(f"publish.platforms.{sec.platform}.enabled"))
                fields.append(f"<div class='check'><input type='hidden' name='enable' value='0'>"
                              f"<input type='checkbox' name='enable' value='1' {'checked' if on else ''}>"
                              f"<span>Публиковать на эту площадку</span></div>")
            fields.append(f"<input type='hidden' name='section' value='{sec.id}'>")
            parts.append(self._form("/save", "".join(fields), "Сохранить и проверить"))
            if sec.id == "youtube":
                parts.append("<div class='row'>" + self._form("/oauth/youtube", "", "Авторизовать YouTube", True) + "</div>")
            if sec.id == "telegram":
                parts.append("<div class='row'>" + self._form("/telegram/discover", "", "Найти канал", True)
                             + self._form("/telegram/local", "", "Переключить бота на локальный сервер", True) + "</div>")
                if self.tg_found and (len(self.tg_found[0]) > 1 or len(self.tg_found[1]) > 1):
                    ch = "".join(f"<option value='{c['id']}'>{html.escape(c['title'])}</option>" for c in self.tg_found[0])
                    pr = "".join(f"<option value='{p['id']}'>{html.escape(p['name'])}</option>" for p in self.tg_found[1])
                    parts.append(self._form("/telegram/choose", f"<label>Канал</label><select name='channel'>{ch}</select>"
                                            f"<label>Чат для алертов</label><select name='private'>{pr}</select>",
                                            "Сохранить выбор"))
            if sec.id == "vk":
                url = self.vk_auth_url()
                login = (f"<p><a href='{html.escape(url)}' target='_blank' rel='noopener'>Войти через VK ↗</a></p>"
                         if url else "<p class='hint'>Сначала сохраните ID приложения VK — появится кнопка входа.</p>")
                parts.append(login + self._form("/vk", "<label>Адрес из строки браузера после входа</label>"
                                                "<input type='password' name='url' autocomplete='off'>", "Сохранить токен"))
        inner = "".join(parts) + msg
        card = f"<div class='card' id='{sec.id}'><h2>{html.escape(sec.title)}{badge}</h2>{inner}</div>"
        if sec.group in ("contour_b", "extra") and not done:
            return f"<details class='card' id='{sec.id}'><summary>{html.escape(sec.title)} — {html.escape(sec.note)}" \
                   f"</summary>{inner}</details>"
        return card


def make_handler(wz: Wizard):
    allowed_hosts = {f"127.0.0.1:{wz.port}", f"localhost:{wz.port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, where: str) -> None:
            self.send_response(303)
            self.send_header("Location", where)
            self.end_headers()

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._send(403, "forbidden")
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            if u.path == "/" and ("code" in q or "error" in q) and "state" in q:
                try:
                    wz.oauth_finish(q)
                except Exception as e:  # noqa: BLE001
                    wz.messages["youtube"] = (False, str(e))
                return self._redirect("/#youtube")
            if u.path == "/status.json":
                return self._send(200, json.dumps(to_json(run_checks(wz.cfg, wz.db)), ensure_ascii=False),
                                  "application/json; charset=utf-8")
            if u.path == "/":
                return self._send(200, wz.page())
            return self._send(404, "not found")

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._send(403, "forbidden")
            n = int(self.headers.get("Content-Length", "0") or 0)
            form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode("utf-8"), keep_blank_values=True))
            if form.get("csrf") != wz.csrf:
                return self._send(403, "устаревшая форма — обновите страницу")
            path = urllib.parse.urlparse(self.path).path
            anchor = {"/keys": "llm", "/save": form.get("section", ""), "/oauth/youtube": "youtube",
                      "/telegram/discover": "telegram", "/telegram/choose": "telegram",
                      "/telegram/local": "telegram", "/vk": "vk"}.get(path, "")
            try:
                if path == "/save":
                    wz.save(form["section"], form)
                elif path == "/keys":
                    wz.import_keys(form.get("provider", "gemini"), form.get("keys", ""))
                elif path == "/oauth/youtube":
                    return self._redirect(wz.oauth_start())
                elif path == "/telegram/discover":
                    wz.tg_discover()
                elif path == "/telegram/choose":
                    wz.tg_choose(form)
                elif path == "/telegram/local":
                    wz.tg_local()
                elif path == "/vk":
                    wz.vk_token(form.get("url", ""))
                else:
                    return self._send(404, "not found")
            except Exception as e:  # noqa: BLE001 — ошибку показываем в секции, мастер продолжает работать
                wz.messages[anchor] = (False, f"{type(e).__name__}: {e}")
            return self._redirect(f"/#{anchor}")

    return Handler


def serve(cfg: Config, db: DB, port: int = 8770, open_browser: bool = False, timeout_s: float = 4 * 3600,
          ready: threading.Event | None = None) -> None:
    wz = Wizard(cfg, db, port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(wz))
    url = f"http://127.0.0.1:{port}/"
    print(f"Мастер настройки: {url}  (Ctrl+C — остановить; сам остановится через {timeout_s / 3600:.0f} ч)")
    if open_browser:
        auth.open_url(url)
    timer = threading.Timer(timeout_s, srv.shutdown)
    timer.daemon = True
    timer.start()
    if ready:
        ready.set()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        timer.cancel()
        srv.server_close()
        time.sleep(0)
