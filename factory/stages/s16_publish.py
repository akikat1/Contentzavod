"""s16 — очередь публикаций.

enqueue(): мастер и шортсы × включённые площадки → строки publications (идемпотентно:
повторный запуск не публикует дважды). Шортсы разносятся по времени.
process_due(): публикует созревшие строки с учётом суточных лимитов площадок;
временные отказы переносятся, постоянные — 3 попытки и алерт.
Синтетический контент и dry-run публикуются только в песочницу.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from ..core import media
from ..core.config import Config
from ..core.context import Ctx
from ..core.db import DB, now
from ..core.workspace import JobDir
from ..providers.publish.alerts import send_alert
from ..providers.publish.base import Deferred, PostMeta, PublishError, Video
from ..providers.publish.registry import enabled_platforms, get_publisher
from ..providers.publish.storage import upload_public

OUTPUTS = ["out/publish_plan.json"]
log = logging.getLogger("factory.publish")


def _variants(job: JobDir) -> dict[str, dict]:
    out = {"master": {"file": "out/master.mp4"}}
    if job.exists("shorts/shorts.json"):
        for i, s in enumerate(job.read_json("shorts/shorts.json"), 1):
            out[f"short_{i:02d}"] = {"file": s["file"], "hook": s["hook"]}
    return out


def enqueue(ctx: Ctx) -> list[dict]:
    cfg = ctx.cfg
    sandbox = cfg.dry_run or ctx.synthetic
    ctx.update_meta(publish_mode="sandbox" if sandbox else "live")
    variants = _variants(ctx.job)
    shorts = [v for v in variants if v.startswith("short_")]
    stagger = float(cfg.get("publish.shorts_stagger_hours", 3)) * 3600
    plan = []
    master_dur = media.duration(ctx.job.p("out", "master.mp4"))
    for name, pc in enabled_platforms(cfg, include_disabled_in_sandbox=sandbox).items():
        wanted = pc.get("variants", ["master"])
        rows = []
        if "master" in wanted:
            if name == "bluesky" and master_dur > float(pc.get("max_video_s", 600)) and shorts:
                rows.append((shorts[0], now()))          # Bluesky не берёт >10 мин — отдаём шортс
            else:
                rows.append(("master", now()))
        if "shorts" in wanted:
            rows += [(s, now() + k * stagger) for k, s in enumerate(shorts)]
        for variant, at in rows:
            ctx.db.execute("INSERT OR IGNORE INTO publications(job_id, platform, variant, status, scheduled_at, "
                           "updated_at) VALUES(?,?,?,?,?,?)", (ctx.job_id, name, variant, "queued", at, now()))
            plan.append({"platform": name, "variant": variant, "at": datetime.fromtimestamp(at).isoformat(
                timespec="minutes")})
    ctx.job.write_json("out/publish_plan.json", {"mode": "sandbox" if sandbox else "live", "items": plan})
    return plan


def _day_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _next_day() -> float:
    d = datetime.now(UTC) + timedelta(days=1)
    return d.replace(hour=0, minute=5, second=0, microsecond=0).timestamp()


def process_due(cfg: Config, db: DB, only_job: str | None = None, platform: str | None = None) -> list[dict]:
    q = "SELECT * FROM publications WHERE status IN ('queued','deferred') AND scheduled_at <= ?"
    args: list = [now()]
    if only_job:
        q += " AND job_id=?"
        args.append(only_job)
    if platform:
        q += " AND platform=?"
        args.append(platform)
    results = []
    for row in db.all(q + " ORDER BY scheduled_at", tuple(args)):
        job = JobDir(cfg, row["job_id"])
        jrow = db.one("SELECT meta FROM jobs WHERE job_id=?", (row["job_id"],))
        jmeta = json.loads(jrow["meta"]) if jrow else {}
        sandbox = jmeta.get("publish_mode") != "live" or cfg.dry_run
        name, variant = row["platform"], row["variant"]
        pc = cfg.section(f"publish.platforms.{name}")
        limit = int(pc.get("daily_limit", 10**6))
        used = db.one("SELECT count FROM platform_daily WHERE platform=? AND day_key=?", (name, _day_key()))
        if not sandbox and used and used["count"] >= limit:
            db.execute("UPDATE publications SET status='deferred', scheduled_at=?, updated_at=? WHERE job_id=? AND "
                       "platform=? AND variant=?", (_next_day(), now(), row["job_id"], name, variant))
            results.append({"platform": name, "variant": variant, "status": "deferred", "why": "суточный лимит"})
            continue
        res = _publish_one(cfg, db, job, name, variant, sandbox)
        results.append(res)
    return results


def _publish_one(cfg: Config, db: DB, job: JobDir, name: str, variant: str, sandbox: bool) -> dict:
    key = (job.job_id, name, variant)
    meta_all = job.read_json("metadata.json")
    pm = meta_all["platforms"].get(name) or meta_all["platforms"]["youtube"]
    info = _variants(job)[variant]
    path = job.p(info["file"])
    pr = media.probe(path)
    vs = next(s for s in pr["streams"] if s["codec_type"] == "video")
    title = pm["title"] if variant == "master" else (info.get("hook") or pm["title"])
    video = Video(path=path, variant=variant, duration=float(pr["format"]["duration"]), width=vs["width"],
                  height=vs["height"], thumbnail=job.p("out", "thumbnail.jpg") if variant == "master" else None,
                  captions=job.p("out", "master.srt") if variant == "master" else None)
    desc = pm["description"]
    if variant != "master":                  # главы мастер-ролика в описании шортса бессмысленны
        desc = re.sub(r"\n\nГлавы:\n.*?(?=\n\n|$)", "", desc, flags=re.S)
    meta = PostMeta(title=title, description=desc, tags=pm.get("tags", []), hashtags=pm.get("hashtags", []))
    pub = get_publisher(cfg, name, sandbox)
    attempts = db.one("SELECT attempts FROM publications WHERE job_id=? AND platform=? AND variant=?", key)["attempts"]
    try:
        if pub.needs_public_url and not sandbox:
            video.public_url = upload_public(cfg, path, f"{job.job_id}/{variant}.mp4")
        res = pub.publish(video, meta)
    except Deferred as e:
        db.execute("UPDATE publications SET status='deferred', scheduled_at=?, attempts=attempts+1, last_error=?, "
                   "updated_at=? WHERE job_id=? AND platform=? AND variant=?", (e.retry_at, str(e)[:500], now(), *key))
        return {"platform": name, "variant": variant, "status": "deferred", "why": str(e)[:120]}
    except PublishError as e:
        failed = attempts + 1 >= 3
        db.execute("UPDATE publications SET status=?, scheduled_at=?, attempts=attempts+1, last_error=?, updated_at=? "
                   "WHERE job_id=? AND platform=? AND variant=?",
                   ("failed" if failed else "queued", now() + 1800 * (attempts + 1), str(e)[:500], now(), *key))
        if failed:
            send_alert(cfg, f"Публикация {name}/{variant} джоба {job.job_id} не удалась: {e}")
        return {"platform": name, "variant": variant, "status": "failed" if failed else "retry", "why": str(e)[:160]}
    except Exception as e:  # noqa: BLE001 — сеть и прочее временное
        db.execute("UPDATE publications SET status='deferred', scheduled_at=?, attempts=attempts+1, last_error=?, "
                   "updated_at=? WHERE job_id=? AND platform=? AND variant=?",
                   (now() + 900, f"{type(e).__name__}: {e}"[:500], now(), *key))
        return {"platform": name, "variant": variant, "status": "deferred", "why": f"{type(e).__name__}: {e}"[:120]}
    db.execute("UPDATE publications SET status='done', url=?, platform_id=?, attempts=attempts+1, last_error=NULL, "
               "updated_at=? WHERE job_id=? AND platform=? AND variant=?", (res.url, res.platform_id, now(), *key))
    if not sandbox:
        db.execute("INSERT INTO platform_daily(platform, day_key, count) VALUES(?,?,1) ON CONFLICT(platform, day_key) "
                   "DO UPDATE SET count=count+1", (name, _day_key()))
    log.info("Опубликовано %s/%s → %s", name, variant, res.url)
    return {"platform": name, "variant": variant, "status": "done", "url": res.url}


def run(ctx: Ctx) -> dict:
    plan = enqueue(ctx)
    res = process_due(ctx.cfg, ctx.db, only_job=ctx.job_id)
    done = [r for r in res if r["status"] == "done"]
    return {"queued": len(plan), "published_now": len(done), "mode": ctx.meta().get("publish_mode"),
            "later": len(plan) - len(res)}
