"""Настройка на ПК пользователя: .env, отчёт готовности, мастер, Telegram, туннель, скрипты."""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml

from factory.core import wizard as wz
from factory.core.config import Config
from factory.core.envfile import mask, read_env, set_env
from factory.core.setup_check import render, run_checks, summary, to_json
from factory.core.telegram_setup import parse_updates
from factory.providers.publish import storage
from factory.providers.publish.base import PublishError

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ .env
def test_set_env_preserves_comments_and_order(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# секреты\nA=1\n\n# b\nB=old\n", encoding="utf-8")
    changed = set_env(p, {"B": "new value", "C": "3", "A": "", "D": "  "}, apply_to_process=False)
    assert changed == ["B", "C"]
    text = p.read_text(encoding="utf-8")
    assert text.splitlines()[:5] == ["# секреты", "A=1", "", "# b", 'B="new value"']
    assert read_env(p) == {"A": "1", "B": "new value", "C": "3"}
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_mask_never_reveals_secret():
    assert mask("") == ""
    assert "abcdefghijkl" not in mask("sk-abcdefghijklmnop")
    assert mask("short") == "•••••"


# ------------------------------------------------------------------ отчёт готовности
@pytest.fixture
def rooted(cfg, tmp_path):
    """Конфиг с корнем во временной папке: .env и config/local.yaml пишутся туда, а не в репозиторий."""
    (tmp_path / "config").mkdir(exist_ok=True)
    c = Config(cfg.data, root=tmp_path)
    for k in ("channel.niche_file", "formats.dir", "voice.library_dir"):
        c.set(k, str(ROOT / cfg.get(k)))
    return c


def test_setup_check_reports_missing_human_steps(rooted, db, monkeypatch):
    for var in ("PEXELS_API_KEY", "YT_CLIENT_ID", "TG_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    items = run_checks(rooted, db)
    by_id = {i.id: i for i in items}
    assert by_id["keys"].status == "fail" and by_id["keys"].category == "human"
    assert by_id["platform_youtube"].status == "fail"
    assert by_id["platform_tiktok"].status == "skip", "площадки после модерации не блокируют готовность"
    s = summary(items)
    assert not s["ready"] and "keys" in s["human_todo"]
    assert "Итог" in render(items)
    assert to_json(items)["summary"]["fail"] == s["fail"]


def test_setup_check_sees_filled_platform(rooted, db, monkeypatch):
    monkeypatch.setenv("BSKY_HANDLE", "me.bsky.social")
    monkeypatch.setenv("BSKY_APP_PASSWORD", "xxxx-xxxx")
    item = next(i for i in run_checks(rooted, db) if i.id == "platform_bluesky")
    assert item.status == "warn" and "выключена" in item.detail


# ------------------------------------------------------------------ мастер
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def wizard(rooted, db, monkeypatch):
    monkeypatch.setattr(wz, "check_section", lambda cfg, sid: (True, "проверено (тест)"))
    monkeypatch.setattr(wz, "probe_keys", lambda cfg, pool, only=None: [])
    port = _free_port()
    ready = threading.Event()
    th = threading.Thread(target=wz.serve, args=(rooted, db, port), kwargs={"timeout_s": 30, "ready": ready},
                          daemon=True)
    th.start()
    ready.wait(5)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(base, timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    page = httpx.get(base + "/").text
    csrf = re.search(r"name='csrf' value='([^']+)'", page).group(1)
    return base, csrf, rooted


def test_wizard_rejects_foreign_origin_and_missing_token(wizard):
    base, csrf, _ = wizard
    assert httpx.get(base + "/", headers={"Host": "evil.example"}).status_code == 403
    assert httpx.post(base + "/save", data={"section": "stock", "PEXELS_API_KEY": "x"}).status_code == 403


def test_wizard_saves_secret_and_enables_platform(wizard):
    base, csrf, cfg = wizard
    r = httpx.post(base + "/save", data={"csrf": csrf, "section": "bluesky", "BSKY_HANDLE": "me.bsky.social",
                                         "BSKY_APP_PASSWORD": "abcd-efgh-ijkl-mnop", "enable": "1"})
    assert r.status_code == 303
    env = read_env(cfg.root / ".env")
    assert env["BSKY_APP_PASSWORD"] == "abcd-efgh-ijkl-mnop"
    local = yaml.safe_load((cfg.root / "config" / "local.yaml").read_text())
    assert local["publish"]["platforms"]["bluesky"]["enabled"] is True
    page = httpx.get(base + "/").text
    assert "abcd-efgh-ijkl-mnop" not in page, "секрет не должен попадать на страницу"


def test_wizard_ignores_fields_of_other_sections(wizard):
    base, csrf, cfg = wizard
    httpx.post(base + "/save", data={"csrf": csrf, "section": "stock", "PEXELS_API_KEY": "p1", "TG_BOT_TOKEN": "zzz"})
    env = read_env(cfg.root / ".env")
    assert env.get("PEXELS_API_KEY") == "p1" and "TG_BOT_TOKEN" not in env


def test_wizard_parses_vk_redirect_url(wizard):
    base, csrf, cfg = wizard
    url = "https://oauth.vk.com/blank.html#access_token=vk1.a.SECRET123&expires_in=0&user_id=42"
    httpx.post(base + "/vk", data={"csrf": csrf, "url": url})
    assert read_env(cfg.root / ".env")["VK_TOKEN"] == "vk1.a.SECRET123"


def test_wizard_imports_llm_keys(wizard):
    base, csrf, cfg = wizard
    httpx.post(base + "/keys", data={"csrf": csrf, "provider": "groq", "keys": "gsk_one\n\ngsk_two\n"})
    data = yaml.safe_load(Path(cfg.get("llm.keys_file")).read_text())
    assert [k["key"] for k in data["keys"] if k["provider"] == "groq"] == ["gsk_one", "gsk_two"]


def test_wizard_status_json(wizard):
    base, _, _ = wizard
    js = httpx.get(base + "/status.json").json()
    assert "summary" in js and js["items"]


# ------------------------------------------------------------------ Telegram
def test_parse_updates_finds_admin_channel_and_private_chat():
    updates = [
        {"my_chat_member": {"chat": {"id": -1001, "type": "channel", "title": "Мой канал", "username": "mych"},
                            "new_chat_member": {"status": "administrator"}}},
        {"my_chat_member": {"chat": {"id": -1002, "type": "channel", "title": "Старый"},
                            "new_chat_member": {"status": "administrator"}}},
        {"my_chat_member": {"chat": {"id": -1002, "type": "channel", "title": "Старый"},
                            "new_chat_member": {"status": "left"}}},
        {"message": {"chat": {"id": 555, "type": "private", "first_name": "Аня"}, "text": "/start"}},
        {"message": {"chat": {"id": 777, "type": "private", "first_name": "Спам"}, "text": "привет"}},
    ]
    channels, privates = parse_updates(updates)
    assert [c["id"] for c in channels] == [-1001]
    assert [p["id"] for p in privates] == [555]


# ------------------------------------------------------------------ туннель
def test_file_server_marks_full_download_across_ranges(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(os.urandom(300_000))
    with storage.FileServer(f) as fs:
        url = f"http://127.0.0.1:{fs.port}{fs.route}"
        assert httpx.get(f"http://127.0.0.1:{fs.port}/wrong/v.mp4").status_code == 404
        assert httpx.head(url).headers["content-length"] == "300000"
        part = httpx.get(url, headers={"Range": "bytes=0-99999"})
        assert part.status_code == 206 and len(part.content) == 100_000
        assert not fs.fetched.is_set()
        rest = httpx.get(url, headers={"Range": "bytes=100000-"})
        assert rest.content == f.read_bytes()[100_000:]
        assert fs.fetched.wait(2), "докачка по частям должна засчитываться как полная загрузка"


def test_public_url_tunnel_mode(tmp_path, cfg, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 1000)

    class FakeProc:
        def terminate(self):
            self.done = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(storage, "start_quick_tunnel", lambda port, binary="cloudflared": (
        FakeProc(), "https://abc-def.trycloudflare.com"))
    monkeypatch.setattr(storage, "_wait_reachable", lambda url, timeout_s=90: None)
    with storage.public_url(cfg, f, "job/master.mp4") as link:
        assert link.url.startswith("https://abc-def.trycloudflare.com/") and link.url.endswith("/v.mp4")
        assert len(link.url.split("/")[3]) >= 20, "в пути должен быть случайный токен"
        assert not link.wait_fetched(0.2)


def test_public_url_off_mode_refuses(tmp_path, cfg):
    cfg.set("publish.remote_storage.mode", "off")
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    with pytest.raises(PublishError), storage.public_url(cfg, f, "k"):
        pass


def test_tunnel_url_regex():
    line = "INF |  https://brave-fox-12.trycloudflare.com                                    |"
    assert storage.TUNNEL_RE.search(line).group(0) == "https://brave-fox-12.trycloudflare.com"


# ------------------------------------------------------------------ скрипты
def test_setup_script_syntax():
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "setup_wsl.sh")], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(ROOT / "scripts" / "setup_wsl.sh")], check=True)


def test_powershell_scripts_have_utf8_bom():
    """Windows PowerShell 5.1 читает .ps1 без BOM в кодировке ANSI — кириллица превратится в мусор."""
    for p in (ROOT / "scripts" / "windows").glob("*.ps1"):
        assert p.read_bytes()[:3] == b"\xef\xbb\xbf", p.name
