"""`factory setup check` — что готово и что осталось, с точным следующим шагом.

Категории: auto — агент сделает сам; human — нужен человек (аккаунты, пароли, согласия);
optional — улучшение, без которого завод работает. `--json` — для разбора агентом.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from . import media
from .config import Config
from .db import DB
from .envfile import env_path, read_env
from .keypool import KeyPool
from .requirements import sections

OK, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"


@dataclass
class Item:
    id: str
    group: str
    title: str
    status: str
    category: str          # auto | human | optional
    detail: str = ""
    action: str = ""


def in_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _run(cmd: list[str], timeout: float = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


# ------------------------------------------------------------------ группы проверок

def system(cfg: Config) -> list[Item]:
    items = [Item("python", "Система", "Python ≥ 3.11", OK if sys.version_info >= (3, 11) else FAIL, "auto",
                  sys.version.split()[0], "установите python3.11+ (scripts/setup_wsl.sh --root)")]
    for binary, why in (("ffmpeg", "монтаж и звук"), ("ffprobe", "анализ медиа"), ("espeak-ng", "офлайн-дно TTS"),
                        ("git", "обновления"), ("fc-match", "шрифты с кириллицей")):
        ok = shutil.which(binary) is not None
        items.append(Item(binary, "Система", f"{binary} — {why}", OK if ok else FAIL, "auto", "",
                          "" if ok else "sudo-часть: scripts/setup_wsl.sh --root"))
    if shutil.which("ffmpeg"):
        need = {"loudnorm", "sidechaincompress", "deesser", "blackdetect", "silencedetect", "ass", "zoompan", "drawtext",
                "alimiter", "ebur128"}
        miss = sorted(need - media.available_filters())
        items.append(Item("ffmpeg_filters", "Система", "Фильтры ffmpeg", FAIL if miss else OK, "auto",
                          f"нет: {miss}" if miss else "все на месте",
                          "поставьте полный ffmpeg из репозитория Ubuntu" if miss else ""))
    return items


def project(cfg: Config) -> list[Item]:
    items = []
    db_ok = cfg.path("paths.db", "data/factory.db").exists()
    gen = cfg.path("paths.assets", "assets") / "generated"
    assets_ok = (gen / "ir" / "room_small.wav").exists() and any((gen / "sfx").glob("*.wav")) \
        if gen.exists() else False
    items.append(Item("init", "Проект", "factory init (база, процедурные ассеты)", OK if db_ok and assets_ok else FAIL,
                      "auto", "", "" if db_ok and assets_ok else "factory init"))
    code, out = _run(["git", "-C", str(cfg.root), "status", "-sb"])
    if code == 0:
        head = out.splitlines()[0] if out else ""
        behind = "behind" in head
        items.append(Item("git", "Проект", "Код актуален", WARN if behind else OK, "auto", head.lstrip("# "),
                          "git pull" if behind else ""))
    keys_file = cfg.path("llm.keys_file", "config/keys.yaml")
    if keys_file.exists() and (keys_file.stat().st_mode & 0o077):
        items.append(Item("keys_perm", "Проект", "Права на config/keys.yaml", WARN, "auto", "файл читают другие",
                          f"chmod 600 {keys_file}"))
    env = env_path(cfg.root)
    if env.exists() and (env.stat().st_mode & 0o077):
        items.append(Item("env_perm", "Проект", "Права на .env", WARN, "auto", "файл читают другие", f"chmod 600 {env}"))
    return items


def gpu(cfg: Config) -> list[Item]:
    from .encoder import nvenc_works  # noqa: PLC0415
    items = []
    code, out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    items.append(Item("nvidia", "GPU", "Видеокарта видна", OK if code == 0 else WARN, "optional",
                      out.strip() if code == 0 else "nvidia-smi не отвечает",
                      "" if code == 0 else "обновите драйвер NVIDIA в Windows (CUDA в WSL идёт из него)"))
    try:
        import torch  # noqa: PLC0415
        cuda = torch.cuda.is_available()
        items.append(Item("torch", "GPU", "PyTorch с CUDA (MiDaS, faster-whisper)", OK if cuda else WARN, "optional",
                          torch.__version__, "" if cuda else "scripts/setup_wsl.sh --gpu"))
    except ImportError:
        items.append(Item("torch", "GPU", "PyTorch с CUDA (MiDaS, faster-whisper)", WARN, "optional",
                          "не установлен — параллакс на эвристической глубине", "scripts/setup_wsl.sh --gpu"))
    if shutil.which("ffmpeg"):
        nv = nvenc_works()
        detail = "аппаратное кодирование" if nv else ("в WSL2 NVENC обычно недоступен — кодирует CPU (libx264), "
                                                     "это штатно" if in_wsl() else "кодирует CPU (libx264)")
        items.append(Item("nvenc", "GPU", "NVENC", OK if nv else SKIP, "optional", detail))
    return items


def keys(cfg: Config, db: DB, probe: bool) -> list[Item]:
    pool = KeyPool.from_config(db, cfg)
    by_prov: dict[str, int] = {}
    for k in pool.keys.values():
        by_prov[k.provider] = by_prov.get(k.provider, 0) + 1
    items = []
    total = sum(by_prov.values())
    items.append(Item("keys", "Ключи LLM", "Ключи LLM: Gemini, Groq, OpenRouter", OK if total else FAIL, "human",
                      ", ".join(f"{p}: {n}" for p, n in sorted(by_prov.items())) or "ни одного",
                      "" if total else "мастер настройки → «Ключи LLM»"))
    if probe and total:
        from .verify import probe_keys  # noqa: PLC0415
        res = probe_keys(cfg, pool)
        alive = sum(1 for r in res if r.status == "ok")
        dead = [r for r in res if r.status == "auth"]
        bad = sorted({f"{r.provider}:{r.model}" for r in res if r.status == "bad_model"})
        items.append(Item("keys_alive", "Ключи LLM", "Ключи отвечают", OK if alive else FAIL, "human",
                          f"живых {alive} из {len(res)}" + (f", недействительных {len(dead)}" if dead else ""),
                          "" if alive else "проверьте ключи в мастере"))
        if bad:
            items.append(Item("models", "Ключи LLM", "Имена моделей актуальны", FAIL, "auto", ", ".join(bad),
                              "factory keys models → поправить llm.providers.*.models в config/local.yaml"))
    else:
        dead = [r for r in pool.status() if r["state"] == "dead"]
        if dead:
            items.append(Item("keys_dead", "Ключи LLM", "Недействительные ключи", WARN, "human",
                              f"{len(dead)} шт.", "замените в мастере; проверить: factory keys probe"))
    return items


def platforms(cfg: Config) -> list[Item]:
    env = read_env(env_path(cfg.root))
    items = []
    for sec in sections():
        if sec.id == "llm":
            continue
        missing = [v.name(cfg) for v in sec.vars if not v.optional
                   and not (os.environ.get(v.name(cfg)) or env.get(v.name(cfg)))]
        enabled = bool(cfg.get(f"publish.platforms.{sec.platform}.enabled")) if sec.platform else True
        group = {"core": "Ассеты", "contour_a": "Площадки", "contour_b": "Площадки (после модерации)",
                 "extra": "Площадки (по желанию)"}[sec.group]
        if not missing:
            status = OK if enabled else WARN
            detail = "секреты есть" + ("" if enabled else ", площадка выключена")
            action = "" if enabled else f"включить: publish.platforms.{sec.platform}.enabled: true в config/local.yaml"
            cat = "auto"
        else:
            status = SKIP if sec.group in ("contour_b", "extra") else FAIL
            detail = "нет: " + ", ".join(missing)
            action = f"мастер настройки → «{sec.title}»"
            cat = "human" if sec.group != "extra" else "optional"
        if sec.note and missing:
            detail += f" ({sec.note})"
        items.append(Item(f"platform_{sec.id}", group, sec.title, status, cat, detail, action))
    return items


def services(cfg: Config) -> list[Item]:
    items = []
    docker = shutil.which("docker") is not None
    items.append(Item("docker", "Сервисы", "Docker установлен", OK if docker else FAIL, "auto", "",
                      "" if docker else "scripts/setup_wsl.sh --root"))
    if docker:
        code, out = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
        items.append(Item("dockerd", "Сервисы", "Docker запущен и доступен пользователю", OK if code == 0 else FAIL,
                          "auto", out.strip()[:80],
                          "" if code == 0 else "sudo systemctl enable --now docker; пользователь в группе docker "
                                               "(перелогиниться: wsl --terminate <дистрибутив>)"))
    tg_token = os.environ.get(cfg.get("publish.platforms.telegram.token_env", "TG_BOT_TOKEN"), "")
    base = cfg.get("publish.platforms.telegram.bot_api_url", "http://127.0.0.1:8081").rstrip("/")
    if "api.telegram.org" not in base:
        try:
            r = httpx.get(f"{base}/bot{tg_token or '0:x'}/getMe", timeout=5)
            up = r.status_code in (200, 401, 404)
            ok = r.status_code == 200 and r.json().get("ok")
            detail = "бот авторизован" if ok else ("сервер работает" + ("" if tg_token else ", токена нет"))
        except httpx.HTTPError:
            up, ok, detail = False, False, "не отвечает"
        items.append(Item("tg_local", "Сервисы", "Локальный Telegram Bot API (файлы до 2 ГБ)", OK if ok else FAIL,
                          "auto", detail,
                          "" if ok else ("docker compose -f deploy/docker-compose.yml --env-file .env up -d "
                                         "telegram-bot-api (нужны TELEGRAM_API_ID/HASH)" if not up else
                                         "мастер → Telegram → «Найти канал» (переключит бота на локальный сервер)")))
    needs_url = [p for p, pc in cfg.section("publish.platforms").items()
                 if pc.get("enabled") and pc.get("needs_public_url")]
    mode = cfg.get("publish.remote_storage.mode", "tunnel")
    if mode == "tunnel":
        has = shutil.which("cloudflared") is not None
        items.append(Item("cloudflared", "Сервисы", "cloudflared (ссылка на видео для Rutube/Instagram/Facebook)",
                          OK if has else (FAIL if needs_url else WARN), "auto",
                          "" if has else f"нужен для: {', '.join(needs_url) or 'пока ни для одной включённой'}",
                          "" if has else "scripts/setup_wsl.sh --root"))
    if cfg.get("publish.platforms.postiz.enabled"):
        base = cfg.get("publish.platforms.postiz.base_url", "http://127.0.0.1:5000/public/v1")
        try:
            up = httpx.get(base.split("/public")[0], timeout=5).status_code < 500
        except httpx.HTTPError:
            up = False
        items.append(Item("postiz", "Сервисы", "Postiz", OK if up else FAIL, "auto", "",
                          "" if up else "docker compose -f deploy/docker-compose.yml --env-file .env --profile postiz up -d"))
    return items


def content(cfg: Config) -> list[Item]:
    moods = ("tense", "calm", "build", "epic", "reflective", "neutral", "dark", "uplifting")
    mdir = cfg.path("sound.music_dir", "assets/music")
    counts = {m: len([p for p in (mdir / m).glob("*") if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a", ".flac")])
              if (mdir / m).exists() else 0 for m in moods}
    empty = [m for m, n in counts.items() if n == 0]
    items = [Item("music", "Контент", "Музыка по настроениям", OK if not empty else WARN, "optional",
                  ", ".join(f"{m}:{n}" for m, n in counts.items()),
                  "" if not empty else f"положите по 5–10 треков в assets/music/<mood>/ (пусто: {', '.join(empty)}); "
                                       f"без них играет процедурная подложка")]
    niche = cfg.path("channel.niche_file", "config/niches/example.yaml")
    items.append(Item("niche", "Контент", "Ниша канала своя, а не пример", WARN if niche.name == "example.yaml" else OK,
                      "human", niche.name, "скопируйте config/niches/example.yaml под свою тему и укажите "
                      "channel.niche_file в config/local.yaml" if niche.name == "example.yaml" else ""))
    free = shutil.disk_usage(cfg.path("paths.workspace", "workspace").parent).free / 2**30
    need = float(cfg.get("schedule.min_free_disk_gb", 40))
    items.append(Item("disk", "Контент", "Свободно на диске", OK if free >= need else FAIL, "human",
                      f"{free:.0f} ГБ (нужно ≥ {need:.0f})", "" if free >= need else "освободите место"))
    return items


def _win_cmd(args: list[str]) -> tuple[int, str]:
    return _run(args, timeout=20)


def windows(cfg: Config) -> list[Item]:
    if not in_wsl():
        return []
    items = []
    code, _ = _win_cmd(["cmd.exe", "/c", "ver"])
    if code != 0:
        return [Item("interop", "Windows", "Связь WSL ↔ Windows", WARN, "auto", "interop выключен",
                     "проверки Windows выполнит агент из PowerShell")]
    for task in ("Contentzavod Tick", "Contentzavod KeepAlive"):
        c, _ = _win_cmd(["schtasks.exe", "/Query", "/TN", task])
        items.append(Item(f"task_{task.split()[-1].lower()}", "Windows (24/7)", f"Задача Планировщика «{task}»",
                          OK if c == 0 else FAIL, "auto", "",
                          "" if c == 0 else "scripts/windows/install-autostart.ps1"))
    c, out = _win_cmd(["powercfg.exe", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"])
    hexes = re.findall(r"0x[0-9a-fA-F]{8}", out)
    if c == 0 and len(hexes) >= 2:
        ac = int(hexes[-2], 16)
        items.append(Item("sleep", "Windows (24/7)", "ПК не засыпает от сети", OK if ac == 0 else FAIL, "auto",
                          "никогда" if ac == 0 else f"засыпает через {ac // 60} мин",
                          "" if ac == 0 else "scripts/windows/install-autostart.ps1 (powercfg)"))
    c, out = _win_cmd(["reg.exe", "query", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon",
                       "/v", "AutoAdminLogon"])
    auto = c == 0 and re.search(r"AutoAdminLogon\s+REG_SZ\s+1", out) is not None
    items.append(Item("autologon", "Windows (24/7)", "Автовход после перезагрузки", OK if auto else FAIL, "human",
                      "" if auto else "без него после перезагрузки или обновления Windows завод не стартует",
                      "" if auto else "Sysinternals Autologon (скрипт install-autostart.ps1 его откроет) — ввести "
                                      "пароль Windows"))
    c, prof = _win_cmd(["cmd.exe", "/c", "echo %USERPROFILE%"])
    if c == 0 and prof.strip():
        cw, lin = _run(["wslpath", "-u", prof.strip().splitlines()[-1].strip()])
        wslcfg = Path(lin.strip()) / ".wslconfig" if cw == 0 else None
        ok = bool(wslcfg and wslcfg.exists() and re.search(r"(?mi)^\s*vmIdleTimeout\s*=\s*-1", wslcfg.read_text(
            encoding="utf-8", errors="replace")))
        items.append(Item("wslconfig", "Windows (24/7)", "WSL не выключается при простое (.wslconfig)",
                          OK if ok else FAIL, "auto", "",
                          "" if ok else "scripts/windows/install-autostart.ps1 (vmIdleTimeout=-1)"))
    systemd = _run(["ps", "-p", "1", "-o", "comm="])[1].strip() == "systemd"
    items.append(Item("systemd", "Windows (24/7)", "systemd в WSL (для Docker)", OK if systemd else FAIL, "auto",
                      "" if systemd else "PID 1 не systemd",
                      "" if systemd else "scripts/setup_wsl.sh --root, затем wsl --shutdown"))
    return items


def run_checks(cfg: Config, db: DB, probe: bool = False) -> list[Item]:
    items: list[Item] = []
    for fn in (system, project, gpu):
        items += fn(cfg)
    items += keys(cfg, db, probe)
    items += platforms(cfg)
    items += services(cfg)
    items += content(cfg)
    items += windows(cfg)
    return items


def summary(items: list[Item]) -> dict:
    todo = [i for i in items if i.status == FAIL]
    return {"ok": sum(1 for i in items if i.status == OK), "fail": len(todo),
            "warn": sum(1 for i in items if i.status == WARN),
            "auto_todo": [i.id for i in todo if i.category == "auto"],
            "human_todo": [i.id for i in todo if i.category == "human"],
            "ready": not todo}


def to_json(items: list[Item]) -> dict:
    return {"items": [asdict(i) for i in items], "summary": summary(items)}


def render(items: list[Item]) -> str:
    icon = {OK: "✔", FAIL: "✘", WARN: "!", SKIP: "·"}
    who = {"auto": "агент", "human": "человек", "optional": "опционально"}
    lines, group = [], None
    for it in items:
        if it.group != group:
            group = it.group
            lines.append(f"\n{group}")
        tail = f" — {it.detail}" if it.detail else ""
        lines.append(f"  {icon[it.status]} {it.title}{tail}")
        if it.status in (FAIL, WARN) and it.action:
            lines.append(f"      → [{who[it.category]}] {it.action}")
    s = summary(items)
    lines.append(f"\nИтог: готово {s['ok']}, осталось {s['fail']} "
                 f"(агент: {len(s['auto_todo'])}, человек: {len(s['human_todo'])}), предупреждений {s['warn']}")
    lines.append("✔ Завод готов к автономной работе" if s["ready"] else
                 "Следующий шаг: сначала пункты [агент], затем мастер настройки для пунктов [человек]")
    return "\n".join(lines)
