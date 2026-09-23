"""s03 — драматургия ДО текста: beat sheet с кривой напряжения, петлями и callback."""
from __future__ import annotations

from ..core.context import LANG_NAMES, Ctx
from ..core.schema import ACTS, REGISTERS, BeatSheet, Research
from ..engagement.beatsheet_rules import act_sequence, register_sequence, validate_beats
from ..engagement.format_rotation import load_format, record_format
from ..engagement.hook_bank import lessons_for_prompt
from ..providers.llm import fixture as fx

OUTPUTS = ["beatsheet.json"]


def run(ctx: Ctx) -> dict:
    topic = ctx.job.read_json("topic.json")
    research = Research.model_validate(ctx.job.read_json("research.json"))
    ct = topic["content_type"]
    fmt = load_format(ctx.cfg, ct)
    rules = ctx.cfg.section("beatsheet")
    target = float(rules.get("target_duration_s", 420))
    skeleton = "\n".join(f"- {b['act']}, tension {b['tension']}, {b['register']}"
                         f"{', cue ' + b['cue'] if b.get('cue') else ''} — {b.get('note', '')}"
                         for b in fmt["skeleton"])
    user = ctx.prompt(
        "beatsheet", topic=topic["topic"], angle=research.angle or topic.get("angle", ""), format_name=fmt["name"],
        format_description=fmt["description"], target_s=str(int(target)),
        tolerance_pct=str(int(float(rules.get("duration_tolerance", 0.2)) * 100)),
        research_summary=research.summary, surprising="; ".join(research.surprising) or "—",
        open_questions="; ".join(research.open_questions) or "—", skeleton=skeleton,
        format_guidance=fmt.get("guidance", "").strip(), lessons=lessons_for_prompt(ctx.db),
        repair_feedback=ctx.repair_feedback("beatsheet"),
        cold_open_max=str(rules.get("cold_open_max_s", 10)), open_loop_max=str(rules.get("open_loop_max_start_s", 45)),
        max_beat=str(rules.get("max_beat_s", 40)), peak_prominence=str(rules.get("peak_prominence", 0.15)),
        acts=", ".join(ACTS), registers=", ".join(REGISTERS), content_type=ct)
    user += f"\nЯзык текстов: {LANG_NAMES.get(ctx.cfg.language, ctx.cfg.language)}."

    def prepare(data: dict) -> dict:
        data["content_type"] = ct
        data.setdefault("topic", topic["topic"])
        return data

    sheet = ctx.llm.json(
        "beatsheet", ctx.system_prompt(), user, BeatSheet, prepare=prepare,
        validate=lambda s: validate_beats(s.beats, rules, target), max_tokens=6000, temperature=0.9,
        fixture=lambda: fx.beatsheet(topic["topic"], ct, fmt["skeleton"], target, rules))
    ctx.job.write_json("beatsheet.json", sheet)
    record_format(ctx.db, ctx.job_id, ct, act_sequence(sheet.beats), register_sequence(sheet.beats))
    return {"beats": len(sheet.beats), "total_s": round(sheet.total_target_s), "content_type": ct,
            "tension": [b.tension for b in sheet.beats]}
