"""s17 — обратная связь: статистика и кривая удержания YouTube → банк хуков и статистика блоков.

Запускается периодически демоном, а не в конвейере джоба: данным нужно 48 часов.
Кривая удержания (audienceWatchRatio по elapsedVideoTimeRatio) из YouTube Analytics API
накладывается на таймлайн: видно, на каком смысловом блоке уходили зрители.
"""
from __future__ import annotations

import datetime as dt
import logging

import httpx
import numpy as np

from ..core.config import Config
from ..core.db import DB, now
from ..core.workspace import JobDir
from ..engagement.hook_bank import update_stats
from ..providers.publish.youtube import access_token

log = logging.getLogger("factory.feedback")


def retention_at(curve: list[tuple[float, float]], t: float, duration: float) -> float | None:
    if not curve or duration <= 0:
        return None
    xs, ys = zip(*curve, strict=True)
    return float(np.interp(min(1.0, t / duration), xs, ys))


def run_global(cfg: Config, db: DB) -> dict:
    yt = cfg.section("publish.platforms.youtube")
    import os  # noqa: PLC0415
    creds = [os.environ.get(yt.get(k, ""), "") for k in ("client_id_env", "client_secret_env", "refresh_token_env")]
    if not all(creds):
        return {"skipped": "нет OAuth-данных YouTube"}
    age = float(cfg.get("schedule.feedback_after_hours", 48)) * 3600
    rows = db.all("SELECT * FROM publications WHERE platform='youtube' AND variant='master' AND status='done' "
                  "AND url NOT LIKE 'sandbox%' AND updated_at < ?", (now() - age,))
    todo = [r for r in rows if db.kv_get(f"feedback:{r['job_id']}") is None]
    if not todo:
        return {"collected": 0}
    tok = access_token(*creds)
    h = {"Authorization": f"Bearer {tok}"}
    done = 0
    for r in todo:
        vid = r["platform_id"]
        try:
            st = httpx.get("https://www.googleapis.com/youtube/v3/videos", headers=h, timeout=30,
                           params={"part": "statistics", "id": vid}).json()
            views = int(((st.get("items") or [{}])[0].get("statistics") or {}).get("viewCount", 0))
            start = dt.date.fromtimestamp(r["updated_at"]).isoformat()
            end = dt.date.today().isoformat()
            rep = httpx.get("https://youtubeanalytics.googleapis.com/v2/reports", headers=h, timeout=60, params={
                "ids": "channel==MINE", "startDate": start, "endDate": end, "metrics": "audienceWatchRatio",
                "dimensions": "elapsedVideoTimeRatio", "filters": f"video=={vid}"}).json()
            curve = sorted((float(a), float(b)) for a, b in rep.get("rows", []))
            avg = httpx.get("https://youtubeanalytics.googleapis.com/v2/reports", headers=h, timeout=60, params={
                "ids": "channel==MINE", "startDate": start, "endDate": end, "metrics": "averageViewPercentage",
                "filters": f"video=={vid}"}).json()
            avg_pct = float((avg.get("rows") or [[None]])[0][0] or 0) / 100 or None
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
            log.warning("feedback %s: %s", r["job_id"], e)
            continue
        job = JobDir(cfg, r["job_id"])
        if not job.exists("timeline.json"):
            continue
        tl = job.read_json("timeline.json")
        dur = tl["duration"]
        update_stats(db, r["job_id"], retention_10s=retention_at(curve, 10, dur),
                     retention_30s=retention_at(curve, 30, dur), avg_view_ratio=avg_pct, views=views)
        beats = {b["block_id"]: b for b in job.read_json("beatsheet.json")["beats"]} if job.exists("beatsheet.json") else {}
        for bl in tl["blocks"]:
            r0, r1 = retention_at(curve, bl["start"], dur), retention_at(curve, bl["end"], dur)
            drop = (r0 - r1) / r0 if r0 and r1 is not None and r0 > 0 else None
            db.execute("INSERT INTO beat_stats(job_id, block_id, act, duration_s, drop_ratio) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(job_id, block_id) DO UPDATE SET drop_ratio=excluded.drop_ratio",
                       (r["job_id"], bl["block_id"], beats.get(bl["block_id"], {}).get("act", "?"),
                        bl["end"] - bl["start"], drop))
        db.kv_set(f"feedback:{r['job_id']}", now())
        done += 1
    return {"collected": done, "pending": len(todo) - done}
