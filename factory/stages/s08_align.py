"""s08 — пословное выравнивание и субтитры.

Цепочка: тайминги от TTS (edge-tts отдаёт настоящие WordBoundary) →
faster-whisper (если стоит, int8 на GPU — ~1 ГБ VRAM) → пропорциональная оценка.
WhisperX здесь не обязателен: для edge-tts точные тайминги уже есть.
"""
from __future__ import annotations

import difflib
import logging

from ..core.context import Ctx
from ..core.gpu_lock import gpu_session
from ..render.subtitles import all_words, write_ass, write_srt
from .s07_humanize import _norm_word

OUTPUTS = ["subs/master.srt", "subs/master.ass"]
log = logging.getLogger("factory.align")


def _whisper_refine(ctx: Ctx, timeline: dict) -> bool:
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except ImportError:
        return False
    cfg = ctx.cfg
    model_holder = {}

    def unload() -> None:
        model_holder.clear()

    with gpu_session(cfg.path("paths.gpu_lock", "data/gpu.lock"), "align", unload):
        try:
            model = WhisperModel(cfg.get("align.whisper_model", "small"), device="auto",
                                 compute_type=cfg.get("align.whisper_compute_type", "int8"))
        except Exception as e:  # noqa: BLE001
            log.warning("faster-whisper не загрузился: %s", e)
            return False
        model_holder["m"] = model
        segs, _ = model.transcribe(str(ctx.job.p("audio", "voice.wav")), language=cfg.language, word_timestamps=True,
                                   vad_filter=False)
        heard = [(w.word.strip(), w.start, w.end) for s in segs for w in (s.words or [])]
    script_words = [(si, wi, w) for si, s in enumerate(timeline["segments"]) for wi, w in enumerate(s["words"])]
    a = [_norm_word(w["w"]) for _, _, w in script_words]
    b = [_norm_word(w) for w, _, _ in heard]
    matched = 0
    for i, j, n in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(n):
            si, wi, w = script_words[i + k]
            _, st, en = heard[j + k]
            w["start"], w["end"] = round(st, 3), round(en, 3)
            matched += 1
    log.info("faster-whisper уточнил %d из %d слов", matched, len(a))
    return matched > 0.6 * len(a)


def run(ctx: Ctx) -> dict:
    timeline = ctx.job.read_json("timeline.json")
    units = ctx.job.read_json("voice_units.json")
    engines = {u["engine"] for s in units for u in s["units"]}
    method = "tts"
    mode = ctx.cfg.get("align.engine", "auto")
    if (mode == "whisper" or (mode == "auto" and engines != {"edge"})) and _whisper_refine(ctx, timeline):
        method = "faster-whisper"
    elif engines != {"edge"}:
        method = "proportional"
    timeline["alignment"] = method
    ctx.job.write_json("timeline.json", timeline)
    words = all_words(timeline)
    sub = ctx.cfg.section("subtitles")
    write_srt(words, ctx.job.p("subs", "master.srt"))
    w, h = int(ctx.cfg.get("render.width", 1920)), int(ctx.cfg.get("render.height", 1080))
    write_ass(words, ctx.job.p("subs", "master.ass"), w, h, font=sub.get("font", "DejaVu Sans"),
              max_words=int(sub.get("max_words_per_line", 5)), base_color=sub.get("base_color", "&H00FFFFFF"),
              emph_color=sub.get("emph_color", "&H0000D7FF"), outline=int(sub.get("outline", 4)), karaoke=False)
    return {"alignment": method, "words": len(words)}
