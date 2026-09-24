"""s04 — текст внутри утверждённой структуры: JSON-контракт сценария."""
from __future__ import annotations

from ..core.context import LANG_NAMES, Ctx
from ..core.schema import BeatSheet, Research, Script
from ..engagement.format_rotation import load_format
from ..engagement.voice_strategy import estimate_seconds
from ..providers.llm import fixture as fx

OUTPUTS = ["script.json"]


def run(ctx: Ctx) -> dict:
    topic = ctx.job.read_json("topic.json")
    sheet = BeatSheet.model_validate(ctx.job.read_json("beatsheet.json"))
    research = Research.model_validate(ctx.job.read_json("research.json"))
    ct = sheet.content_type
    fmt = load_format(ctx.cfg, ct)
    cps = float(ctx.cfg.get("voice.est_chars_per_second", 14.5))
    strategy_hint = ctx.cfg.get(f"voice.strategies.{ct}", "single")
    secondary_hint = {
        "dual_narrator": "В этом формате второй нарратор ОБЯЗАТЕЛЕН: отдай ему 2–4 блока целиком.",
        "dual_opposing": "Второй нарратор — оппонент: он озвучивает мифы (блоки myth).",
    }.get(strategy_hint if isinstance(strategy_hint, str) else "", "В этом формате второй нарратор не обязателен.")
    beats_json = sheet.model_dump(include={"beats"})["beats"]
    user = ctx.prompt(
        "script", topic=topic["topic"], format_name=fmt["name"], format_guidance=fmt.get("guidance", "").strip(),
        language_name=LANG_NAMES.get(ctx.cfg.language, ctx.cfg.language), beatsheet=beats_json,
        facts="\n".join(f"{f.id} [{f.source_id}]: {f.claim}" for f in research.facts),
        sources="\n".join(f"{s.id}: {s.title} {s.url}" for s in research.sources),
        repair_feedback=ctx.repair_feedback("script"), cps=str(cps), secondary_hint=secondary_hint,
        moods="tense, calm, build, epic, reflective, neutral, dark, uplifting",
        title_styles="question, number, statement, contrarian, story, how", job_id=ctx.job_id, content_type=ct,
        language=ctx.cfg.language, target_s=str(int(sheet.target_duration_s)))

    def prepare(data: dict) -> dict:
        # структуру и служебные поля не доверяем копированию моделью
        data["beats"] = beats_json
        data.update(job_id=ctx.job_id, content_type=ct, language=ctx.cfg.language,
                    target_duration_s=sheet.target_duration_s)
        data.setdefault("topic", topic["topic"])
        data["sources"] = [s.model_dump() for s in research.sources]
        data.pop("voice_plan", None)
        return data

    def validate(sc: Script) -> list[str]:
        est = sum(estimate_seconds(s.text, cps) for s in sc.segments)
        tgt = sheet.target_duration_s
        if not (0.65 * tgt <= est <= 1.35 * tgt):
            return [f"текст рассчитан примерно на {est:.0f} с, а ролик должен идти {tgt:.0f} с — "
                    f"{'добавь содержания' if est < tgt else 'сократи'} (≈{cps} символов в секунду)"]
        return []

    sc = ctx.llm.json("script", ctx.system_prompt(), user, Script, prepare=prepare, validate=validate,
                      max_tokens=16000, temperature=0.85,
                      fixture=lambda: fx.script(ctx.job_id, topic["topic"], ct, ctx.cfg.language, sheet.model_dump(),
                                                research.model_dump(), cps))
    ctx.job.write_json("script.json", sc)
    est = sum(estimate_seconds(s.text, cps) for s in sc.segments)
    return {"segments": len(sc.segments), "est_duration_s": round(est), "speakers": len(sc.speakers),
            "roles": sorted({s.role for s in sc.segments})}
