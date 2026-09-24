"""s05 — Voice Strategy: стратегия, нормализация ролей по инвариантам, назначение голосов.
Полностью детерминированно: модель размечает роли, но сколько голосов и какие — решает код."""
from __future__ import annotations

from ..core.context import Ctx
from ..core.schema import Script
from ..engagement import voice_strategy as vs

OUTPUTS = ["voiceplan.json"]


def run(ctx: Ctx) -> dict:
    sc = Script.model_validate(ctx.job.read_json("script.json"))
    original_roles = {s.id: s.role for s in sc.segments}
    actual = _actual_durations(ctx, sc)
    strategy, reasons = vs.choose_strategy(sc, ctx.cfg)
    if actual:
        reasons.append("ремонт по QA: инварианты считаются по реальным длительностям озвучки")
    segs, norm_log = vs.normalize_roles(sc, strategy, ctx.cfg, actual)
    sc = sc.model_copy(update={"segments": segs})
    plan = vs.assign_voices(sc, segs, strategy, ctx.library, ctx.cfg, ctx.db)
    plan.decisions = reasons + plan.decisions
    sc.voice_plan = plan
    Script.model_validate(sc.model_dump())            # контракт по-прежнему валиден
    ctx.job.write_json("script.json", sc)
    problems = vs.check_invariants(sc, ctx.cfg, actual)
    changed = sum(1 for s in sc.segments if original_roles.get(s.id) != s.role)
    report = {"strategy": strategy, "reasons": reasons, "assignments": plan.assignments,
              "durations": "реальные" if actual else "оценка по тексту",
              "speaker_voices": plan.speaker_voices, "decisions": plan.decisions, "normalization": norm_log,
              "roles_changed": changed, "switches": vs.explain_switches(sc, ctx.cfg, actual),
              "invariant_problems": problems}
    ctx.job.write_json("voiceplan.json", report)
    vs.record_voice_history(ctx.db, sc, plan, ctx.cfg.get("channel.series", "main"))
    if problems:
        raise RuntimeError("Инварианты Voice Strategy нарушены после нормализации: " + "; ".join(problems))
    return {"strategy": strategy, "voices": len(set(plan.assignments.values()) | set(plan.speaker_voices.values())),
            "roles_changed": changed, "switches": len(report["switches"])}


def _actual_durations(ctx: Ctx, sc: Script) -> dict[str, float] | None:
    """Если QA вернула ремонт голосов и озвучка этого же текста уже есть — берём реальные длительности."""
    if not ctx.repair_feedback("voiceplan") or not ctx.job.exists("timeline.json"):
        return None
    import hashlib  # noqa: PLC0415
    tl = ctx.job.read_json("timeline.json")
    h = hashlib.sha256("|".join(f"{s.id}:{s.text}" for s in sc.segments).encode()).hexdigest()[:16]
    if tl.get("script_hash") != h:
        return None
    return {s["id"]: s["end"] - s["start"] for s in tl["segments"]}
