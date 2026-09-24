"""Оркестратор: прогон джоба по стадиям с идемпотентностью и циклом авто-ремонта.

Стадия, уже завершённая и с существующими артефактами, повторно не запускается.
QA-гейт при провале указывает, с какой стадии переделывать и что именно
исправить; фидбэк передаётся в промты, стадии с этой и далее инвалидируются.
После qa.max_cycles неудач джоб уходит в карантин с алертом.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from importlib import import_module

from .config import Config
from .context import Ctx
from .db import DB, now
from .keypool import KeyPool
from .monitor import ResourceMonitor
from .workspace import new_job_id

log = logging.getLogger("factory.pipeline")

STAGES: list[tuple[str, str]] = [
    ("trends", "s01_trends"), ("research", "s02_research"), ("beatsheet", "s03_beatsheet"),
    ("script", "s04_script"), ("voiceplan", "s05_voiceplan"), ("voice", "s06_voice"),
    ("humanize", "s07_humanize"), ("align", "s08_align"), ("visualplan", "s09_visualplan"),
    ("assets", "s10_assets"), ("sound", "s11_sound"), ("render", "s12_render"), ("clips", "s13_clips"),
    ("metadata", "s14_metadata"), ("qa", "s15_qa"), ("publish", "s16_publish"),
]
STAGE_NAMES = [s for s, _ in STAGES]


class StageFailed(RuntimeError):
    pass


class Quarantined(RuntimeError):
    pass


def stage_module(name: str):
    mod = dict(STAGES)[name]
    return import_module(f"factory.stages.{mod}")


def create_job(db: DB, cfg: Config, topic: str | None = None, content_type: str | None = None) -> str:
    job_id = new_job_id(topic or "auto")
    meta = {"topic_given": topic, "format_given": content_type, "offline": cfg.offline, "dry_run": cfg.dry_run}
    db.execute("INSERT INTO jobs(job_id, topic, content_type, status, created_at, updated_at, meta) "
               "VALUES(?,?,?,?,?,?,?)", (job_id, topic or "", content_type, "new", now(), now(),
                                         json.dumps(meta, ensure_ascii=False)))
    return job_id


def stage_done(db: DB, ctx: Ctx, name: str) -> bool:
    row = db.one("SELECT status FROM stage_runs WHERE job_id=? AND stage=?", (ctx.job_id, name))
    if not row or row["status"] != "done":
        return False
    outputs = getattr(stage_module(name), "OUTPUTS", [])
    return all(ctx.job.p(o).exists() for o in outputs)


def invalidate_from(db: DB, job_id: str, stage: str) -> None:
    idx = STAGE_NAMES.index(stage)
    for name in STAGE_NAMES[idx:]:
        db.execute("DELETE FROM stage_runs WHERE job_id=? AND stage=?", (job_id, name))


def run_stage(ctx: Ctx, name: str, force: bool = False) -> dict:
    db = ctx.db
    if not force and stage_done(db, ctx, name):
        return {"skipped": True}
    prev = db.one("SELECT attempt FROM stage_runs WHERE job_id=? AND stage=?", (ctx.job_id, name))
    attempt = (prev["attempt"] + 1) if prev else 1
    db.execute("INSERT INTO stage_runs(job_id, stage, status, attempt, started_at) VALUES(?,?,?,?,?) "
               "ON CONFLICT(job_id, stage) DO UPDATE SET status='running', attempt=excluded.attempt, "
               "started_at=excluded.started_at, error=NULL", (ctx.job_id, name, "running", attempt, now()))
    t0 = time.time()
    log.info("▶ %s", name)
    try:
        summary = stage_module(name).run(ctx) or {}
    except Exception as e:
        db.execute("UPDATE stage_runs SET status='failed', finished_at=?, error=? WHERE job_id=? AND stage=?",
                   (now(), f"{type(e).__name__}: {e}\n{traceback.format_exc()[-3000:]}", ctx.job_id, name))
        raise StageFailed(f"стадия {name}: {type(e).__name__}: {e}") from e
    summary["seconds"] = round(time.time() - t0, 1)
    db.execute("UPDATE stage_runs SET status='done', finished_at=?, summary=? WHERE job_id=? AND stage=?",
               (now(), json.dumps(summary, ensure_ascii=False, default=str), ctx.job_id, name))
    log.info("✔ %s за %.1f с", name, summary["seconds"])
    return summary


def run_job(cfg: Config, db: DB, job_id: str, until: str | None = None, force: set[str] | None = None,
            pool: KeyPool | None = None) -> dict:
    ctx = Ctx(cfg=cfg, db=db, job_id=job_id, pool=pool)
    db.execute("UPDATE jobs SET status='running', updated_at=? WHERE job_id=?", (now(), job_id))
    force = force or set()
    if force:                        # переделка стадии делает устаревшими все, что ниже по конвейеру
        invalidate_from(db, job_id, min(force, key=STAGE_NAMES.index))
    max_cycles = int(cfg.get("qa.max_cycles", 3))
    summaries: dict[str, dict] = {}
    with ResourceMonitor() as mon:
        i = 0
        while i < len(STAGE_NAMES):
            name = STAGE_NAMES[i]
            try:
                summaries[name] = run_stage(ctx, name, force=name in force)
            except StageFailed:
                db.execute("UPDATE jobs SET status='failed', updated_at=? WHERE job_id=?", (now(), job_id))
                raise
            if ctx.llm.used_fixture:
                ctx.mark_synthetic("offline-fixture LLM")
            if name == "qa":
                report = ctx.job.read_json("qa_report.json")
                if not report["passed"]:
                    cycles = db.one("SELECT qa_cycles FROM jobs WHERE job_id=?", (job_id,))["qa_cycles"] + 1
                    db.execute("UPDATE jobs SET qa_cycles=? WHERE job_id=?", (cycles, job_id))
                    if cycles >= max_cycles:
                        db.execute("UPDATE jobs SET status='quarantined', updated_at=? WHERE job_id=?",
                                   (now(), job_id))
                        _alert(cfg, f"Джоб {job_id} в карантине после {cycles} циклов QA: "
                                    + "; ".join(report["failed"]))
                        raise Quarantined(f"{job_id}: QA не пройдена за {cycles} циклов")
                    signature = sorted(p for c in report["checks"] if not c["passed"] for p in c["problems"])
                    if signature and signature == ctx.meta().get("last_qa_signature"):
                        db.execute("UPDATE jobs SET status='quarantined', updated_at=? WHERE job_id=?", (now(), job_id))
                        _alert(cfg, f"Джоб {job_id} в карантине: ремонт не изменил результат QA — "
                                    + "; ".join(signature[:3]))
                        raise Quarantined(f"{job_id}: ремонт не дал эффекта ({'; '.join(signature[:2])})")
                    ctx.update_meta(last_qa_signature=signature)
                    stage = report["repair_from"]
                    log.warning("QA не пройдена (%s) — ремонт с стадии %s, цикл %d/%d",
                                ", ".join(report["failed"]), stage, cycles, max_cycles)
                    invalidate_from(db, job_id, stage)
                    i = STAGE_NAMES.index(stage)
                    continue
            if until and name == until:
                break
            i += 1
    manifest = {"job_id": job_id, "stages": summaries, "resources": mon.report(), "notes": ctx.notes,
                "llm_calls": ctx.llm.calls, "synthetic": ctx.meta().get("synthetic", [])}
    ctx.job.write_json("manifest.json", manifest)
    final = "done" if (until is None or until == STAGE_NAMES[-1]) else "running"
    db.execute("UPDATE jobs SET status=?, updated_at=? WHERE job_id=?", (final, now(), job_id))
    return manifest


def _alert(cfg: Config, text: str) -> None:
    log.error("АЛЕРТ: %s", text)
    try:
        from ..providers.publish.alerts import send_alert  # noqa: PLC0415
        send_alert(cfg, text)
    except Exception as e:  # noqa: BLE001 — алерт не должен ронять конвейер
        log.warning("алерт не отправлен: %s", e)
