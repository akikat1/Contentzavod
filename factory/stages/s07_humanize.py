"""s07 — сборка голосовой дорожки и гуманизация (слои 2–4).

Паузы по смыслу (фраза / абзац / блок / драматическая тишина перед кульминацией),
вдохи, акцент +2 дБ на словах [emph], FX-цепочки ролей, RVC (опционально),
реверберация комнаты, постобработка и нормализация. На выходе voice.wav и
timeline.json — единая шкала времени для субтитров, монтажа и звука.
"""
from __future__ import annotations

import difflib
import random
import re

import numpy as np

from ..core import media
from ..core.context import Ctx
from ..core.schema import Script
from ..providers.tts import humanize as hz
from ..providers.tts.rvc import RVCUnavailable, convert

OUTPUTS = ["timeline.json", "audio/voice.wav"]


def _norm_word(w: str) -> str:
    return re.sub(r"[^\w]", "", w.lower())


def _map_emph(script_words: list, engine_words: list) -> list[tuple[str, float, float, bool]]:
    """Сопоставить слова сценария (с флагом emph) и тайминги движка — они могут токенизироваться по-разному."""
    sw = [_norm_word(w) for w, _ in script_words]
    ew = [_norm_word(w) for w, _, _ in engine_words]
    out = []
    sm = difflib.SequenceMatcher(None, sw, ew, autojunk=False)
    emph_by_e = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            emph_by_e[b + k] = (script_words[a + k][0], bool(script_words[a + k][1]))
    for j, (w, s, e) in enumerate(engine_words):
        text, emph = emph_by_e.get(j, (w, False))
        out.append((text, s, e, emph))
    return out


def run(ctx: Ctx) -> dict:
    cfg = ctx.cfg
    sc = Script.model_validate(ctx.job.read_json("script.json"))
    units_manifest = ctx.job.read_json("voice_units.json")
    mode = cfg.get("voice.humanize", "basic")
    humanize = mode != "off"
    rng = random.Random(ctx.job_id)
    pauses = cfg.section("voice.pauses_ms")
    bcfg = cfg.section("voice.breath")
    breaths = hz.BreathBank(cfg.path("paths.assets", "assets") / "generated" / "breaths")
    beats = {b.block_id: b for b in sc.beats}

    def pause(kind: str) -> float:
        lo, hi = pauses.get(kind, [200, 300])
        return rng.uniform(lo, hi) / 1000 if humanize else (lo + hi) / 2000

    track: list[np.ndarray] = []
    t = 0.0
    timeline_segs, silences = [], []
    prev_block = None
    rvc_used = False
    seg_dir = ctx.job.p("audio", "segments")
    seg_dir.mkdir(parents=True, exist_ok=True)

    for si, seg in enumerate(units_manifest):
        # --- пауза перед сегментом ---
        if si > 0:
            gap = pause("block") if seg["block_id"] != prev_block else pause("paragraph")
            if seg["block_id"] != prev_block and beats[seg["block_id"]].music_cue == "silence_before":
                d = pause("dramatic")
                silences.append({"start": t + gap * 0.5, "end": t + gap * 0.5 + d, "reason": "перед кульминацией",
                                 "block_id": seg["block_id"]})
                gap += d
            track.append(media.silence(gap))
            t += gap
        prev_block = seg["block_id"]

        # --- юниты сегмента ---
        parts: list[np.ndarray] = []
        words: list[dict] = []
        local = 0.0
        for ui, u in enumerate(seg["units"]):
            pcm = media.load_audio(ctx.job.p(u["wav"]))
            pcm, cut = media.trim_silence(pcm)
            udur = len(pcm) / media.SR
            gap = u["pause_before_ms"] / 1000
            if ui > 0:
                gap += pause("phrase") if seg["units"][ui - 1]["ends_sentence"] else 0.06
            want_breath = u["breath_before"] or (
                humanize and bcfg.get("enabled", True) and u["n_words"] >= int(bcfg.get("min_words", 12))
                and (ui > 0 or si > 0) and rng.random() < float(bcfg.get("probability", 0.55)))
            if want_breath:
                lo_db, hi_db = bcfg.get("gain_db", [-30, -24])
                b = breaths.pick(rng, rng.uniform(lo_db, hi_db))
                if b is not None:
                    need = len(b) / media.SR + 0.08
                    gap = max(gap, need)
                    lead = media.silence(gap - need)
                    parts += [lead, b.astype(np.float32), media.silence(0.08)]
                    local += gap
                    gap = 0.0
            if gap > 0:
                parts.append(media.silence(gap))
                local += gap
            mapped = _map_emph(u["script_words"], [(w, s - cut, e - cut) for w, s, e in u["words"]])
            emph_spans = [(max(0.0, s), max(0.0, e)) for _, s, e, em in mapped if em]
            if emph_spans and humanize:
                pcm = hz.emphasize(pcm, emph_spans, 2.0)
            for w, s, e, em in mapped:
                words.append({"w": w, "start": round(local + max(0.0, s), 3),
                              "end": round(local + min(udur, max(s + 0.05, e)), 3), "emph": em})
            parts.append(pcm)
            local += udur
        seg_pcm = np.concatenate(parts) if parts else media.silence(0.1)
        raw = media.save_wav(seg_dir / f"{seg['seg_id']}_raw.wav", seg_pcm)
        cur = raw
        # --- слой 4: RVC для нарраторских ролей ---
        if mode == "rvc" and seg["role"] in cfg.get("voice.rvc.roles", []):
            try:
                cur = convert(cur, seg_dir / f"{seg['seg_id']}_rvc.wav", cfg.get("voice.rvc.command"),
                              cfg.path("voice.rvc.model"), float(cfg.get("voice.rvc.chunk_s", 30)))
                rvc_used = True
            except RVCUnavailable as e:
                ctx.notes.append(f"RVC недоступен, голос без конверсии: {e}")
                mode = "basic"
        # --- FX роли (радио 1940-х, телефон, мысль...) ---
        if seg["fx_chain"]:
            cur = hz.apply_fx(cur, seg_dir / f"{seg['seg_id']}_fx.wav", seg["fx_chain"], seed=si)
        seg_pcm = media.load_audio(cur)
        seg_pcm = seg_pcm[: max(len(seg_pcm), 1)]
        start = t
        track.append(seg_pcm)
        t += len(seg_pcm) / media.SR
        timeline_segs.append({"id": seg["seg_id"], "block_id": seg["block_id"], "role": seg["role"],
                              "speaker": seg["speaker"], "voice_id": seg["voice_id"], "start": round(start, 3),
                              "end": round(t, 3), "words": [{**w, "start": round(w["start"] + start, 3),
                                                             "end": round(w["end"] + start, 3)} for w in words]})
        tail = seg["trailing_pause_ms"] / 1000
        if tail:
            track.append(media.silence(tail))
            t += tail
    track.append(media.silence(0.6))
    t += 0.6
    voice = np.concatenate(track)

    # --- слой 3: комната + постобработка ---
    room = cfg.section("voice.room_ir")
    if humanize and room.get("enabled", True):
        irp = cfg.path("voice.room_ir.file")
        if irp.exists():
            voice = hz.convolve_room(voice, media.load_audio(irp), float(room.get("wet", 0.08)))
    raw_path = media.save_wav(ctx.job.p("audio", "voice_raw.wav"), voice)
    chain = cfg.get("voice.post_chain", "") if humanize else ""
    hz.post_process(raw_path, ctx.job.p("audio", "voice.wav"), chain, float(cfg.get("voice.voice_lufs", -16)))
    duration = media.duration(ctx.job.p("audio", "voice.wav"))

    # --- слоты блоков: вся шкала без дыр, границы — по середине пауз ---
    blocks = []
    for b in sc.beats:
        ss = [s for s in timeline_segs if s["block_id"] == b.block_id]
        blocks.append({"block_id": b.block_id, "speech_start": ss[0]["start"], "speech_end": ss[-1]["end"]})
    for i, bl in enumerate(blocks):
        bl["start"] = 0.0 if i == 0 else round((blocks[i - 1]["speech_end"] + bl["speech_start"]) / 2, 3)
    for i, bl in enumerate(blocks):
        bl["end"] = round(duration, 3) if i == len(blocks) - 1 else blocks[i + 1]["start"]
    import hashlib  # noqa: PLC0415
    script_hash = hashlib.sha256("|".join(f"{s.id}:{s.text}" for s in sc.segments).encode()).hexdigest()[:16]
    timeline = {"duration": round(duration, 3), "script_hash": script_hash, "segments": timeline_segs, "blocks": blocks,
                "planned_silences": silences, "humanize": mode, "rvc": rvc_used, "alignment": "tts"}
    ctx.job.write_json("timeline.json", timeline)
    return {"duration_s": round(duration, 1), "silences": len(silences), "humanize": mode,
            "breaths": "on" if humanize else "off"}
