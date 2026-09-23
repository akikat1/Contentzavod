"""Разовая авторизация площадок — единственный шаг, который человек делает руками.

`factory auth youtube` открывает согласие Google в браузере, ловит код на локальном
порту и печатает refresh token для .env. Важно: пока OAuth-приложение в статусе
«Testing», Google отзывает refresh token через 7 дней — переведите приложение
в «In production» (для личного использования проверка Google не нужна).
"""
from __future__ import annotations

import http.server
import os
import secrets
import threading
import urllib.parse
import webbrowser

import httpx

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.force-ssl",
          "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/yt-analytics.readonly"]


def youtube(client_id: str | None = None, client_secret: str | None = None, port: int = 8765) -> str:
    client_id = client_id or os.environ.get("YT_CLIENT_ID", "")
    client_secret = client_secret or os.environ.get("YT_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        raise SystemExit("Задайте YT_CLIENT_ID и YT_CLIENT_SECRET (OAuth-клиент типа «Desktop app»)")
    redirect = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state})
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("Готово, окно можно закрыть.".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    th = threading.Thread(target=srv.handle_request, daemon=True)
    th.start()
    print(f"Откройте в браузере (если не открылось само):\n{url}\n")
    webbrowser.open(url)
    th.join(timeout=600)
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"Авторизация не завершена: {got.get('error', 'нет кода')}")
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "code": got["code"], "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect,
        "grant_type": "authorization_code"})
    r.raise_for_status()
    token = r.json().get("refresh_token")
    if not token:
        raise SystemExit("Google не выдал refresh_token — отзовите доступ приложения в аккаунте и повторите")
    return token
