"""Автономный режим: один «тик» — публикации, фидбэк, производство по плану дня, уборка.
systemd timer вызывает `factory daemon --once` каждые 30 минут; тик идемпотентен."""
from __future__ import annotations

import logging
import shutil
from datetime import datetime

from .config import Config
from .db import DB, now
from .keypool import KeyPool
from .pipeline import Quarantined, StageFailed, create_job, run_job
from .workspace import cleanup_intermediates, enforce_history_limit

log = logging.getLogger("factory.daemon")


def _alert(cfg: Config, text: str) -> None:
    try:
        from ..providers.publish.alerts import send_alert  # noqa: PLC0415
        send_alert(cfg, text)
    except Exception:  # noqa: BLE001
        pass


def tick(cfg: Config, db: DB, pool: KeyPool) -> dict:
    from ..stages import s16_publish, s17_feedback  # noqa: PLC0415
    out: dict = {"at": datetime.now().isoformat(timespec="seconds")}
    out["published"] = s16_publish.process_due(cfg, db)

    last_fb = db.kv_get("daemon:last_feedback", 0)
    if now() - last_fb > 6 * 3600:
        try:
            out["feedback"] = s17_feedback.run_global(cfg, db)
        except Exception as e:  # noqa: BLE001
            out["feedback"] = f"ошибка: {e}"
        db.kv_set("daemon:last_feedback", now())

    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    made_today = db.one("SELECT COUNT(*) AS n FROM jobs WHERE created_at >= ? AND status != 'failed'", (midnight,))["n"]
    free_gb = shutil.disk_usage(cfg.path("paths.workspace", "workspace").parent).free / 2**30
    stale = db.one("SELECT job_id FROM jobs WHERE status='running' AND updated_at < ? ORDER BY created_at LIMIT 1",
                   (now() - 2 * 3600,))
    target = int(cfg.get("schedule.videos_per_day", 2))
    job_id = None
    if free_gb < float(cfg.get("schedule.min_free_disk_gb", 40)):
        out["production"] = f"пропуск: на диске {free_gb:.0f} ГБ"
        _alert(cfg, out["production"])
    elif stale:
        job_id = stale["job_id"]
        out["production"] = f"продолжаю прерванный {job_id}"
    elif made_today < target:
        job_id = create_job(db, cfg)
        out["production"] = f"новый джоб {job_id} ({made_today + 1}/{target} за сегодня)"
    else:
        out["production"] = f"план дня выполнен ({made_today}/{target})"
    if job_id:
        try:
            m = run_job(cfg, db, job_id, pool=pool)
            out["job"] = {"job_id": job_id, "resources": m["resources"]}
        except Quarantined as e:
            out["job"] = f"карантин: {e}"
        except StageFailed as e:
            db.execute("UPDATE jobs SET status='failed', updated_at=? WHERE job_id=?", (now(), job_id))
            _alert(cfg, f"Джоб {job_id} упал: {e}")
            out["job"] = f"ошибка: {e}"

    if cfg.get("schedule.cleanup_after_publish", True):
        freed = 0
        for r in db.all("SELECT job_id FROM publications GROUP BY job_id HAVING SUM(status NOT IN "
                        "('done','skipped','failed')) = 0"):
            freed += cleanup_intermediates(cfg, r["job_id"])
        removed = enforce_history_limit(cfg, int(cfg.get("schedule.keep_jobs", 20)))
        out["cleanup"] = {"freed_gb": round(freed / 2**30, 2), "removed_jobs": len(removed)}
    return out
