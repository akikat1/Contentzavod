"""CLI контент-завода: `factory <команда>`. Полный список — `factory --help`."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from .config import load_config
from .db import DB

log = logging.getLogger("factory.cli")


def _ctx(args):
    from .monitor import setup_logging  # noqa: PLC0415
    overrides = dict(kv.split("=", 1) for kv in (args.set or []))
    if getattr(args, "offline", False):
        overrides["runtime.offline"] = "true"
    if getattr(args, "dry_run", False):
        overrides["runtime.dry_run"] = "true"
    cfg = load_config(overrides)
    setup_logging(cfg.get("runtime.log_level", "INFO"), cfg.path("paths.logs", "data/logs") / "factory.log")
    db = DB(cfg.path("paths.db", "data/factory.db"))
    return cfg, db


def _pool(cfg, db):
    from .keypool import KeyPool  # noqa: PLC0415
    return KeyPool.from_config(db, cfg)


# ------------------------------------------------------------------ init / doctor

def cmd_init(args) -> int:
    cfg, db = _ctx(args)
    from ..providers.audio.procedural import bootstrap  # noqa: PLC0415
    made = bootstrap(cfg.path("paths.assets", "assets") / "generated")
    keys = cfg.path("llm.keys_file", "config/keys.yaml")
    if not keys.exists():
        shutil.copyfile(cfg.root / "config" / "keys.example.yaml", keys)
        keys.chmod(0o600)
        print(f"Создан {keys} — впишите ключи (он в .gitignore)")
    for mood in ("tense", "calm", "build", "epic", "reflective", "neutral", "dark", "uplifting"):
        (cfg.path("sound.music_dir", "assets/music") / mood).mkdir(parents=True, exist_ok=True)
    print(f"База: {db.path}\nПроцедурных ассетов создано: {len(made)}")
    return 0


def cmd_doctor(args) -> int:
    cfg, db = _ctx(args)
    from . import encoder, media  # noqa: PLC0415
    checks = []

    def add(name, ok, detail=""):
        checks.append((name, ok, detail))

    add("ffmpeg", media.have("ffmpeg"))
    need = {"loudnorm", "sidechaincompress", "afir", "deesser", "blackdetect", "silencedetect", "ass", "zoompan",
            "drawtext"}
    miss = need - media.available_filters() if media.have("ffmpeg") else need
    add("фильтры ffmpeg", not miss, f"нет: {sorted(miss)}" if miss else "все на месте")
    add("кодек", True, encoder.describe(cfg.get("render.encoder", "auto")))
    add("espeak-ng (офлайн-дно TTS)", media.have("espeak-ng"))
    add("piper (офлайн TTS)", media.have("piper"), "необязательно")
    try:
        import faster_whisper  # noqa: F401, PLC0415
        add("faster-whisper", True)
    except ImportError:
        add("faster-whisper", False, "необязательно: для edge-tts тайминги слов уже точные")
    try:
        import torch  # noqa: PLC0415
        cuda = torch.cuda.is_available()
        vram = torch.cuda.get_device_properties(0).total_memory / 2**30 if cuda else 0
        add("CUDA", cuda, f"{vram:.1f} ГБ VRAM" if cuda else "параллакс будет на эвристической глубине")
    except ImportError:
        add("torch", False, "необязательно: MiDaS для параллакса, faster-whisper на GPU")
    free = shutil.disk_usage(cfg.path("paths.workspace", "workspace").parent).free / 2**30
    add("свободно на диске", free > float(cfg.get("schedule.min_free_disk_gb", 40)), f"{free:.0f} ГБ")
    pool = _pool(cfg, db)
    add("ключей в пуле", bool(pool.keys), f"{len(pool.keys)}")
    for name, ok, detail in checks:
        print(f"{'✔' if ok else '✘'} {name:32} {detail}")
    return 0 if all(ok for n, ok, _ in checks if n in ("ffmpeg", "фильтры ffmpeg")) else 1


# ------------------------------------------------------------------ keys

def cmd_keys(args) -> int:
    cfg, db = _ctx(args)
    from .keypool import append_keys_file  # noqa: PLC0415
    if args.action == "import":
        secrets = Path(args.file).read_text(encoding="utf-8").splitlines()
        n = append_keys_file(cfg.path("llm.keys_file", "config/keys.yaml"), args.provider, secrets,
                             args.account_prefix)
        print(f"Добавлено ключей {args.provider}: {n}")
        return 0
    pool = _pool(cfg, db)
    if args.action == "probe":
        return _probe(cfg, pool)
    if args.action == "models":
        from .verify import list_models  # noqa: PLC0415
        for prov, names in list_models(cfg, pool).items():
            print(f"\n{prov} ({len(names)}):")
            for n in names:
                print(f"  {n}")
        return 0
    rows = pool.status()
    if not rows:
        print("Пул пуст. Заполните config/keys.yaml или FACTORY_KEYS_<PROVIDER>=k1,k2")
        return 1
    print(f"{'ключ':40} {'сост.':8} {'запр/сут':>12} {'токены/сут':>18} {'картинки':>10} здоровье")
    for r in rows:
        rq = f"{r['day_requests']}/{r['rpd'] or '∞'}"
        tk = f"{r['day_tokens']}/{r['tpd'] or '∞'}"
        im = f"{r['day_images']}/{r['ipd']}" if r["ipd"] else "—"
        print(f"{r['key_id'][:40]:40} {r['state']:8} {rq:>12} {tk:>18} {im:>10} {r['health']}"
              + (f"  ⚠ {r['last_error'][:60]}" if r["last_error"] else ""))
    alive = sum(1 for r in rows if r["state"] == "ok")
    print(f"\nЖивых: {alive} из {len(rows)}")
    return 0


def _probe(cfg, pool) -> int:
    from .verify import probe_keys  # noqa: PLC0415
    icons = {"ok": "✔", "rate_limited": "⏳", "auth": "✘", "bad_model": "?", "error": "✘", "skipped": "·"}
    results = probe_keys(cfg, pool)
    for r in results:
        extra = f"{r.seconds:5.1f} с" if r.status == "ok" else r.message
        print(f"{icons[r.status]} {r.key_id:40} {r.model:40} {extra}")
    if any(r.status == "bad_model" for r in results):
        print("\nЕсть отвергнутые модели: `factory keys models` покажет актуальные имена, "
              "впишите их в config/local.yaml → llm.providers.<провайдер>.models")
    ok = sum(1 for r in results if r.status == "ok")
    print(f"\nРабочих ключей: {ok} из {len(results)}")
    return 0 if ok else 1


# ------------------------------------------------------------------ run / resume / stage

def cmd_run(args) -> int:
    cfg, db = _ctx(args)
    from .pipeline import Quarantined, StageFailed, create_job, run_job  # noqa: PLC0415
    job_id = create_job(db, cfg, args.topic, args.format)
    print(f"Джоб: {job_id}")
    try:
        manifest = run_job(cfg, db, job_id, until=args.until, pool=_pool(cfg, db))
    except (StageFailed, Quarantined) as e:
        print(f"✘ {e}")
        return 2
    _print_manifest(cfg, job_id, manifest)
    return 0


def cmd_resume(args) -> int:
    cfg, db = _ctx(args)
    from .pipeline import Quarantined, StageFailed, run_job  # noqa: PLC0415
    force = set(args.force.split(",")) if args.force else set()
    db.execute("UPDATE jobs SET qa_cycles=0 WHERE job_id=?", (args.job,))     # ручной resume — новый бюджет ремонта
    from .context import Ctx  # noqa: PLC0415
    Ctx(cfg=cfg, db=db, job_id=args.job).update_meta(last_qa_signature=None)
    try:
        manifest = run_job(cfg, db, args.job, until=args.until, force=force, pool=_pool(cfg, db))
    except (StageFailed, Quarantined) as e:
        print(f"✘ {e}")
        return 2
    _print_manifest(cfg, args.job, manifest)
    return 0


def cmd_stage(args) -> int:
    cfg, db = _ctx(args)
    from .context import Ctx  # noqa: PLC0415
    from .pipeline import run_stage  # noqa: PLC0415
    ctx = Ctx(cfg=cfg, db=db, job_id=args.job, pool=_pool(cfg, db))
    print(json.dumps(run_stage(ctx, args.name, force=True), ensure_ascii=False, indent=2, default=str))
    return 0


def _print_manifest(cfg, job_id: str, manifest: dict) -> None:
    root = cfg.path("paths.workspace", "workspace") / "jobs" / job_id
    print(f"\n✔ Готово: {root}")
    for p in sorted((root / "out").glob("*")):
        print(f"   {p.relative_to(root)}  {p.stat().st_size / 2**20:.1f} МБ")
    for p in sorted((root / "shorts").glob("*.mp4")):
        print(f"   {p.relative_to(root)}  {p.stat().st_size / 2**20:.1f} МБ")
    res = manifest.get("resources", {})
    print(f"   пик RSS: {res.get('peak_rss_mb')} МБ, пик VRAM: {res.get('peak_vram_mb') or 'н/д'} МБ")
    if manifest.get("synthetic"):
        print(f"   ⚠ синтетический контент ({', '.join(manifest['synthetic'])}) — на площадки не уйдёт")


# ------------------------------------------------------------------ отладочные команды

def cmd_beatsheet(args) -> int:
    cfg, db = _ctx(args)
    from .context import Ctx  # noqa: PLC0415
    from .pipeline import create_job, run_stage  # noqa: PLC0415
    job_id = create_job(db, cfg, args.topic, args.format)
    ctx = Ctx(cfg=cfg, db=db, job_id=job_id, pool=_pool(cfg, db))
    for st in ("trends", "research", "beatsheet"):
        run_stage(ctx, st)
    sheet = ctx.job.read_json("beatsheet.json")
    print(f"Джоб {job_id}: {sheet['content_type']}, хук: {sheet['hook']}\n")
    t = 0.0
    for b in sheet["beats"]:
        bar = "█" * int(b["tension"] * 20)
        loop = (f" ⟳{b['opens_loop']}" if b.get("opens_loop") else "") + \
               (f" ✓{b['closes_loop']}" if b.get("closes_loop") else "") + \
               (f" ↩{b['callback_to']}" if b.get("callback_to") else "")
        print(f"{int(t // 60):02d}:{int(t % 60):02d} {b['block_id']} {b['act']:12} {b['visual_register']:15} "
              f"{b['music_cue']:15} {bar:20} {b['tension']:.2f}{loop}")
        t += b["target_duration_s"]
    return 0


def cmd_voiceplan(args) -> int:
    cfg, db = _ctx(args)
    root = cfg.path("paths.workspace", "workspace") / "jobs" / args.job
    rep = json.loads((root / "voiceplan.json").read_text(encoding="utf-8"))
    print(f"Стратегия: {rep['strategy']}")
    for r in rep["reasons"]:
        print(f"  • {r}")
    print("\nНазначения:")
    for d in rep["decisions"]:
        if d not in rep["reasons"]:
            print(f"  • {d}")
    if args.explain:
        print(f"\nНормализация ролей ({rep['roles_changed']} изменений):")
        for n in rep["normalization"] or ["  изменений не потребовалось"]:
            print(f"  • {n}")
        print("\nПереключения голоса:")
        for s in rep["switches"] or ["  переключений нет — один голос"]:
            print(f"  {s}")
        if rep.get("invariant_problems"):
            print("\n✘ Нарушения:", rep["invariant_problems"])
        else:
            print("\n✔ Все инварианты Voice Strategy соблюдены")
    return 0


def cmd_tts(args) -> int:
    cfg, db = _ctx(args)
    import random  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    from ..core import media  # noqa: PLC0415
    from ..providers.audio.procedural import bootstrap  # noqa: PLC0415
    from ..providers.tts import humanize as hz  # noqa: PLC0415
    from ..providers.tts.markup import parse  # noqa: PLC0415
    from ..providers.tts.router import TTSRouter  # noqa: PLC0415
    from ..providers.tts.voice_library import VoiceLibrary  # noqa: PLC0415
    bootstrap(cfg.path("paths.assets", "assets") / "generated")
    lib = VoiceLibrary.for_config(cfg)
    voice = lib.get(args.voice or lib.defaults["main_narrator"])
    tts = TTSRouter(cfg)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    modes = [args.humanize] if args.humanize != "all" else ["off", "basic"]
    for mode in modes:
        humanize = mode != "off"
        parts = []
        rng = random.Random(1)
        for i, u in enumerate(parse(args.text).units):
            pros = hz.unit_prosody(voice.prosody, None, u, voice.base_f0_hz, cfg.section("voice.sentence_jitter"),
                                   seed=f"demo:{i}", humanize=humanize)
            res = tts.synthesize(u.text, voice, pros, out_dir / f"_u{i}.wav")
            pcm, _ = media.trim_silence(media.load_audio(res.wav))
            gap = u.pause_before_ms / 1000 + (rng.uniform(0.12, 0.3) if humanize else 0.2) * (i > 0)
            parts += [media.silence(gap), pcm]
        pcm = np.concatenate(parts)
        if humanize:
            pcm = hz.convolve_room(pcm, media.load_audio(cfg.path("voice.room_ir.file")), 0.08)
        raw = media.save_wav(out_dir / f"_raw_{mode}.wav", pcm)
        final = out_dir / f"demo_{mode}.wav"
        hz.post_process(raw, final, cfg.get("voice.post_chain") if humanize else "", -16)
        print(f"{mode:6} → {final} ({media.duration(final):.1f} с, движки: {tts.used})")
    for p in out_dir.glob("_*.wav"):
        p.unlink()
    return 0


def cmd_qa(args) -> int:
    cfg, db = _ctx(args)
    from .context import Ctx  # noqa: PLC0415
    from .pipeline import run_stage  # noqa: PLC0415
    ctx = Ctx(cfg=cfg, db=db, job_id=args.job, pool=_pool(cfg, db))
    run_stage(ctx, "qa", force=True)
    rep = ctx.job.read_json("qa_report.json")
    for c in rep["checks"]:
        print(f"{'✔' if c['passed'] else '✘'} {c['name']:14} {c['summary']}")
        for p in c["problems"][:6]:
            print(f"     – {p}")
    print("\nИТОГ:", "ПРИНЯТО" if rep["passed"] else f"ОТКЛОНЕНО, ремонт с стадии {rep['repair_from']}")
    return 0 if rep["passed"] else 3


def cmd_publish(args) -> int:
    cfg, db = _ctx(args)
    from ..stages import s16_publish  # noqa: PLC0415
    if args.job:
        from .context import Ctx  # noqa: PLC0415
        ctx = Ctx(cfg=cfg, db=db, job_id=args.job, pool=_pool(cfg, db))
        s16_publish.enqueue(ctx)
    res = s16_publish.process_due(cfg, db, only_job=args.job, platform=args.platform)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_feedback(args) -> int:
    cfg, db = _ctx(args)
    from ..stages import s17_feedback  # noqa: PLC0415
    print(json.dumps(s17_feedback.run_global(cfg, db), ensure_ascii=False, indent=2))
    return 0


def cmd_daemon(args) -> int:
    cfg, db = _ctx(args)
    from .daemon import tick  # noqa: PLC0415
    while True:
        print(json.dumps(tick(cfg, db, _pool(cfg, db)), ensure_ascii=False, indent=2, default=str))
        if args.once:
            return 0
        time.sleep(float(args.interval))


def cmd_auth(args) -> int:
    cfg, db = _ctx(args)
    from . import auth  # noqa: PLC0415
    from .envfile import env_path, set_env  # noqa: PLC0415
    if args.platform == "youtube":
        tok = auth.youtube(port=args.port)
        set_env(env_path(cfg.root), {cfg.get("publish.platforms.youtube.refresh_token_env", "YT_REFRESH_TOKEN"): tok})
        print("✔ Refresh token сохранён в .env")
        return 0
    from .telegram_setup import fetch_updates, parse_updates, switch_to_local  # noqa: PLC0415
    token = os.environ.get(cfg.get("publish.platforms.telegram.token_env", "TG_BOT_TOKEN"), "")
    if not token:
        print("✘ Нет TG_BOT_TOKEN — сначала мастер настройки, секция Telegram")
        return 1
    if args.platform == "telegram-local":
        print(switch_to_local(cfg, token))
        return 0
    updates, where = fetch_updates(cfg, token)
    channels, privates = parse_updates(updates)
    print(f"Через {where}: каналов {len(channels)}, личных чатов с /start {len(privates)}")
    for c in channels:
        print(f"  канал {c['id']}  {c['title']}  @{c.get('username') or '—'}")
    for p in privates:
        print(f"  чат   {p['id']}  {p['name']}")
    upd = {}
    if len(channels) == 1:
        upd[cfg.get("publish.platforms.telegram.chat_id_env", "TG_CHANNEL_ID")] = str(channels[0]["id"])
    if len(privates) == 1:
        upd[cfg.get("alerts.telegram.chat_id_env", "TG_ADMIN_CHAT_ID")] = str(privates[0]["id"])
        from .config import set_local  # noqa: PLC0415
        set_local(cfg, "alerts.telegram.enabled", True)
    set_env(env_path(cfg.root), upd)
    if upd:
        print(f"✔ Сохранено в .env: {', '.join(upd)}")
    if not channels:
        print("✋ Бот не видит канал: добавьте его администратором и напишите в канал любое сообщение")
    if not privates:
        print("✋ Напишите боту /start в личку — туда будут приходить алерты")
    return 0 if channels else 1


def cmd_setup(args) -> int:
    cfg, db = _ctx(args)
    if args.action == "check":
        from .setup_check import render, run_checks, summary, to_json  # noqa: PLC0415
        items = run_checks(cfg, db, probe=args.probe)
        print(json.dumps(to_json(items), ensure_ascii=False, indent=1) if args.json else render(items))
        return 0 if summary(items)["ready"] else 4
    if args.action == "verify":
        from .requirements import sections  # noqa: PLC0415
        from .verify import CHECKS, check_section  # noqa: PLC0415
        bad = 0
        for sec in sections():
            if sec.id not in CHECKS or (args.section and sec.id != args.section):
                continue
            ok, msg = check_section(cfg, sec.id)
            bad += not ok
            print(f"{'✔' if ok else '✘'} {sec.title:55} {msg}")
        return 1 if bad else 0
    from .wizard import serve  # noqa: PLC0415
    serve(cfg, db, port=args.port, open_browser=args.open, timeout_s=args.timeout)
    return 0


def cmd_env(args) -> int:
    cfg, _ = _ctx(args)
    from .envfile import env_path, mask, read_env, set_env  # noqa: PLC0415
    path = env_path(cfg.root)
    if args.action == "set":
        import getpass  # noqa: PLC0415
        value = getpass.getpass(f"{args.key} (ввод скрыт): ") if sys.stdin.isatty() else sys.stdin.readline()
        set_env(path, {args.key: value.strip()})
        print(f"{args.key} сохранён в {path}")
        return 0
    from .requirements import sections  # noqa: PLC0415
    env = read_env(path)
    for sec in sections():
        for v in sec.vars:
            name = v.name(cfg)
            val = env.get(name) or os.environ.get(name, "")
            shown = (val if not v.secret else mask(val)) if val else "—"
            print(f"{sec.id:10} {name:24} {shown}")
    return 0


def cmd_cleanup(args) -> int:
    cfg, db = _ctx(args)
    from .workspace import cleanup_intermediates, enforce_history_limit  # noqa: PLC0415
    freed = 0
    for r in db.all("SELECT DISTINCT job_id FROM publications WHERE status='done'"):
        freed += cleanup_intermediates(cfg, r["job_id"])
    removed = enforce_history_limit(cfg, int(cfg.get("schedule.keep_jobs", 20)))
    print(f"Освобождено {freed / 2**30:.2f} ГБ, удалено старых джобов: {len(removed)}")
    return 0


def cmd_jobs(args) -> int:
    cfg, db = _ctx(args)
    for r in db.all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (args.limit,)):
        meta = json.loads(r["meta"])
        flag = " ⚠synthetic" if meta.get("synthetic") else ""
        print(f"{r['job_id']:48} {r['status']:12} {r['content_type'] or '—':13} {r['topic'][:50]}{flag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="factory", description="Бесплатный автономный контент-завод")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="переопределить параметр конфига")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        sp.add_argument("--set", action="append", metavar="KEY=VALUE", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
        return sp

    add("init", cmd_init, "создать БД, процедурные ассеты и заготовку keys.yaml")
    add("doctor", cmd_doctor, "проверить окружение: ffmpeg, NVENC, TTS, GPU, диск, ключи")
    sp = add("keys", cmd_keys, "пул ключей: status | probe | models | import")
    sp.add_argument("action", choices=["status", "probe", "models", "import"])
    sp.add_argument("--provider")
    sp.add_argument("--file")
    sp.add_argument("--account-prefix", default="acc")
    for name, fn, help_ in (("run", cmd_run, "создать джоб и прогнать конвейер"),
                            ("beatsheet", cmd_beatsheet, "показать драматургию для темы (тема → исследование → beat sheet)")):
        sp = add(name, fn, help_)
        sp.add_argument("--topic")
        sp.add_argument("--format", help="explainer|investigation|listicle|story|myth_vs_fact|timeline|comparison|case_study")
        sp.add_argument("--offline", action="store_true", help="без сети: llama.cpp/фикстура, espeak, процедурные ассеты")
        sp.add_argument("--dry-run", action="store_true", help="публикация в песочницу")
        sp.add_argument("--until", help="остановиться после стадии")
        sp.add_argument("--show", action="store_true", help=argparse.SUPPRESS)
    sp = add("resume", cmd_resume, "продолжить джоб (идемпотентно)")
    sp.add_argument("job")
    sp.add_argument("--until")
    sp.add_argument("--force", help="переделать стадии через запятую")
    sp.add_argument("--offline", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("stage", cmd_stage, "перезапустить одну стадию")
    sp.add_argument("job")
    sp.add_argument("name")
    sp.add_argument("--offline", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("voiceplan", cmd_voiceplan, "показать Voice Strategy джоба")
    sp.add_argument("job")
    sp.add_argument("--explain", action="store_true")
    sp = add("tts", cmd_tts, "демо озвучки: сравнить humanize off/basic")
    sp.add_argument("action", choices=["demo"])
    sp.add_argument("--text", default="[emph]Девять минут[/emph]. [pause:450] Столько у них было. А почему никто не "
                                      "услышал? [breath] Потому что сообщение сочли [emph]неважным[/emph]. [pause:700] "
                                      "[slow]И вот что было дальше.[/slow]")
    sp.add_argument("--voice")
    sp.add_argument("--humanize", default="all", choices=["off", "basic", "all"])
    sp.add_argument("--out", default="workspace/tts_demo")
    sp.add_argument("--offline", action="store_true")
    sp = add("qa", cmd_qa, "прогнать QA-гейт по джобу")
    sp.add_argument("job")
    sp.add_argument("--offline", action="store_true")
    sp = add("publish", cmd_publish, "поставить джоб в очередь публикаций и обработать созревшие")
    sp.add_argument("job", nargs="?")
    sp.add_argument("--platform")
    sp.add_argument("--dry-run", action="store_true")
    add("feedback", cmd_feedback, "собрать статистику опубликованных роликов → банк хуков")
    sp = add("daemon", cmd_daemon, "автономный режим: план на день, публикации, фидбэк, очистка")
    sp.add_argument("--once", action="store_true", help="один тик (для systemd timer)")
    sp.add_argument("--interval", default="900")
    sp.add_argument("--offline", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("auth", cmd_auth, "youtube — OAuth; telegram — найти канал; telegram-local — перевести бота на локальный сервер")
    sp.add_argument("platform", choices=["youtube", "telegram", "telegram-local"])
    sp.add_argument("--port", type=int, default=8765)
    sp = add("setup", cmd_setup, "check — что готово и что осталось; wizard — мастер в браузере; verify — живая проверка секретов")
    sp.add_argument("action", choices=["check", "wizard", "verify"])
    sp.add_argument("section", nargs="?", help="verify: только эта секция (stock, youtube, telegram, …)")
    sp.add_argument("--json", action="store_true", help="check: вывод для агента")
    sp.add_argument("--probe", action="store_true", help="check: проверить ключи живым вызовом")
    sp.add_argument("--port", type=int, default=8770, help="wizard: порт")
    sp.add_argument("--open", action="store_true", help="wizard: открыть в браузере (в WSL — в браузере Windows)")
    sp.add_argument("--timeout", type=float, default=4 * 3600, help="wizard: остановиться через N секунд")
    sp = add("env", cmd_env, "секреты в .env: check (маскированно) | set KEY (скрытый ввод)")
    sp.add_argument("action", choices=["check", "set"])
    sp.add_argument("key", nargs="?")
    add("cleanup", cmd_cleanup, "удалить промежуточные файлы опубликованных джобов")
    sp = add("jobs", cmd_jobs, "список джобов")
    sp.add_argument("--limit", type=int, default=20)

    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
