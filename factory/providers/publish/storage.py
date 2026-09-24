"""Публичная ссылка на видео для площадок, которые забирают файл по URL (Rutube, Instagram, Facebook).

Режимы (publish.remote_storage.mode):
  tunnel — по умолчанию: локальный HTTP-сервер со случайным токеном в пути + Cloudflare quick tunnel
           (`cloudflared tunnel --url`). Без аккаунта и без банковской карты. Туннель живёт, пока
           площадка не скачает файл целиком (или до таймаута).
  r2     — Cloudflare R2 (S3 API); для включения R2 Cloudflare требует привязать карту.
  off    — такие площадки публиковать не получится.
"""
from __future__ import annotations

import http.server
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from ...core.config import Config
from .base import PublishError

log = logging.getLogger("factory.storage")
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class PublicLink:
    def __init__(self, url: str, fetched: threading.Event | None = None):
        self.url = url
        self._fetched = fetched

    def wait_fetched(self, timeout_s: float) -> bool:
        """Дождаться, пока площадка скачает файл целиком. Для R2 — сразу True."""
        if self._fetched is None or timeout_s <= 0:
            return True
        return self._fetched.wait(timeout_s)


class FileServer:
    """Отдаёт ровно один файл по секретному пути; поддерживает HEAD и Range, отмечает полную отдачу."""

    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size
        self.token = secrets.token_urlsafe(18)
        self.route = f"/{self.token}/{urllib.parse.quote(path.name)}"
        self.fetched = threading.Event()
        self.served: dict[tuple[int, int], bool] = {}
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug(fmt, *args)

            def _range(self) -> tuple[int, int] | None:
                m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
                if not m:
                    return None
                a = int(m.group(1)) if m.group(1) else server.size - int(m.group(2))
                b = int(m.group(2)) if m.group(1) and m.group(2) else server.size - 1
                return max(0, a), min(server.size - 1, b)

            def _headers(self, code: int, length: int, rng: tuple[int, int] | None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if rng:
                    self.send_header("Content-Range", f"bytes {rng[0]}-{rng[1]}/{server.size}")
                self.end_headers()

            def do_HEAD(self):  # noqa: N802
                if self.path != server.route:
                    self.send_error(404)
                    return
                self._headers(200, server.size, None)

            def do_GET(self):  # noqa: N802
                if self.path != server.route:
                    self.send_error(404)
                    return
                rng = self._range()
                a, b = rng if rng else (0, server.size - 1)
                self._headers(206 if rng else 200, b - a + 1, rng)
                sent = 0
                try:
                    with open(server.path, "rb") as fh:
                        fh.seek(a)
                        left = b - a + 1
                        while left > 0:
                            chunk = fh.read(min(1 << 20, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            sent += len(chunk)
                            left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                if sent == b - a + 1:
                    server.mark(a, b)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def mark(self, a: int, b: int) -> None:
        """Файл считается скачанным, когда отданные отрезки покрывают его целиком."""
        self.served[(a, b)] = True
        covered, pos = 0, 0
        for x, y in sorted(self.served):
            if x > pos:
                break
            pos = max(pos, y + 1)
            covered = pos
        if covered >= self.size:
            self.fetched.set()

    def __enter__(self) -> FileServer:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def start_quick_tunnel(port: int, binary: str = "cloudflared", timeout_s: float = 60) -> tuple[subprocess.Popen, str]:
    if not shutil.which(binary):
        raise PublishError("нет cloudflared — установите: scripts/setup_wsl.sh --root")
    proc = subprocess.Popen([binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    found: list[str] = []

    def reader() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            m = TUNNEL_RE.search(line)
            if m and not found:
                found.append(m.group(0))

    threading.Thread(target=reader, daemon=True).start()
    t0 = time.time()
    while not found and time.time() - t0 < timeout_s and proc.poll() is None:
        time.sleep(0.2)
    if not found:
        proc.terminate()
        raise PublishError("cloudflared не выдал адрес туннеля")
    return proc, found[0]


def _wait_reachable(url: str, timeout_s: float = 90) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if httpx.head(url, timeout=10, follow_redirects=True).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise PublishError("туннель не стал доступен из интернета")


@contextmanager
def public_url(cfg: Config, path: Path, key: str) -> Iterator[PublicLink]:
    rs = cfg.section("publish.remote_storage")
    mode = rs.get("mode", "tunnel") if rs.get("mode") else ("r2" if rs.get("enabled") else "tunnel")
    if mode == "off":
        raise PublishError("площадке нужна публичная ссылка, а publish.remote_storage.mode = off")
    if mode == "r2":
        url = upload_public(cfg, path, key)
        try:
            yield PublicLink(url)
        finally:
            delete_public(cfg, key)
        return
    with FileServer(path) as fs:
        proc, base = start_quick_tunnel(fs.port, rs.get("cloudflared", "cloudflared"))
        try:
            url = base + fs.route
            _wait_reachable(url)
            log.info("Временная ссылка для площадки готова (%s)", base)
            yield PublicLink(url, fs.fetched)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _s3(cfg: Config):
    rs = cfg.section("publish.remote_storage")
    try:
        import boto3  # noqa: PLC0415
    except ImportError as e:
        raise PublishError("для R2 нужен boto3: pip install -e .[r2]") from e
    return boto3.client("s3", endpoint_url=os.environ.get(rs.get("endpoint_env", "R2_ENDPOINT")),
                        aws_access_key_id=os.environ.get(rs.get("access_key_env", "R2_ACCESS_KEY_ID")),
                        aws_secret_access_key=os.environ.get(rs.get("secret_key_env", "R2_SECRET_ACCESS_KEY")),
                        region_name="auto"), rs


def upload_public(cfg: Config, path: Path, key: str) -> str:
    s3, rs = _s3(cfg)
    s3.upload_file(str(path), rs["bucket"], key, ExtraArgs={"ContentType": "video/mp4"})
    base = rs.get("public_base_url", "").rstrip("/")
    if not base:
        raise PublishError("remote_storage.public_base_url не задан")
    return f"{base}/{key}"


def delete_public(cfg: Config, key: str) -> None:
    try:
        s3, rs = _s3(cfg)
        s3.delete_object(Bucket=rs["bucket"], Key=key)
    except Exception:  # noqa: BLE001 — уборка буфера не критична
        pass
