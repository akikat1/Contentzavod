"""s06 — посегментный синтез: голос назначается ролью сегмента, просодия — голосом,
сценарием и пофразной вариацией (слой 2). Результат — юниты с пословными таймингами."""
from __future__ import annotations

from ..core.context import Ctx
from ..core.schema import Script
from ..engagement.voice_strategy import voice_for_segment
from ..providers.tts.humanize import unit_prosody
from ..providers.tts.markup import parse
from ..providers.tts.router import TTSRouter

OUTPUTS = ["voice_units.json"]


def run(ctx: Ctx) -> dict:
    sc = Script.model_validate(ctx.job.read_json("script.json"))
    if sc.voice_plan is None:
        raise RuntimeError("нет voice_plan — стадия voiceplan не отработала")
    tts = TTSRouter(ctx.cfg)
    humanize = ctx.cfg.get("voice.humanize", "basic") != "off"
    jitter = ctx.cfg.section("voice.sentence_jitter")
    out_dir = ctx.job.p("audio", "units")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for seg in sc.segments:
        vid = voice_for_segment(seg, sc.voice_plan)
        voice = ctx.library.get(vid)
        plan = parse(seg.text)
        units = []
        for i, u in enumerate(plan.units):
            pros = unit_prosody(voice.prosody, seg.prosody.model_dump() if seg.prosody else None, u,
                                voice.base_f0_hz, jitter, seed=f"{ctx.job_id}:{seg.id}:{i}", humanize=humanize)
            wav = out_dir / f"{seg.id}_{i:02d}.wav"
            res = tts.synthesize(u.text, voice, pros, wav)
            units.append({"wav": str(wav.relative_to(ctx.job.root)), "duration": res.duration, "engine": res.engine,
                          "words": res.words, "script_words": [[w.text, w.emph] for w in u.words],
                          "pause_before_ms": u.pause_before_ms, "breath_before": u.breath_before,
                          "ends_sentence": u.ends_sentence, "n_words": len(u.words)})
        manifest.append({"seg_id": seg.id, "block_id": seg.block_id, "role": seg.role, "speaker": seg.speaker,
                         "voice_id": vid, "fx_chain": seg.fx_chain or voice.fx_chain, "units": units,
                         "trailing_pause_ms": plan.trailing_pause_ms})
    ctx.job.write_json("voice_units.json", manifest)
    if "espeak" in tts.used and not ctx.cfg.offline:
        ctx.notes.append("часть фраз озвучена резервным espeak-ng — проверьте доступность edge-tts")
    return {"segments": len(manifest), "units": sum(len(m["units"]) for m in manifest), "engines": tts.used}
