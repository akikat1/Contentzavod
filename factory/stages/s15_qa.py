"""s15 — QA-гейт: восемь проверок вместо человека на приёмке.

Провал любой проверки → отчёт с самой ранней стадией ремонта и конкретным
фидбэком; оркестратор инвалидирует стадии и перезапускает конвейер с неё.
"""
from __future__ import annotations

from dataclasses import asdict

from ..core.context import Ctx
from ..core.pipeline import STAGE_NAMES
from ..qa.checks import run_all, write_feedback

OUTPUTS = ["qa_report.json"]


def run(ctx: Ctx) -> dict:
    results = run_all(ctx)
    failed = [r for r in results if not r.passed]
    repair = min((r.repair_from for r in failed if r.repair_from), key=STAGE_NAMES.index, default=None)
    if failed:
        write_feedback(ctx.job.p("repair_feedback.json"), failed)
    report = {"passed": not failed, "failed": [r.name for r in failed], "repair_from": repair,
              "checks": [asdict(r) for r in results], "notes": sum((r.notes for r in results), [])}
    ctx.job.write_json("qa_report.json", report)
    return {"passed": not failed, "failed": report["failed"], "repair_from": repair}
