"""s11 — звуковой дизайн.

- музыка по актам (music_plan), кроссфейд на смене настроения;
- ducking: музыка приседает под голос (огибающая голоса с атакой/спадом — sidechain в numpy);
- полная тишина в запланированных окнах перед кульминацией — самый сильный приём в звуке;
- SFX: whoosh на смене акта, riser перед откровением, impact на шоковом факте и счётчике;
- финальная нормализация двухпроходным loudnorm до −14 LUFS / −1.5 dBTP.
"""
from __future__ import annotations

import logging
import random
import re
from pathlib import Path

import numpy as np

from ..core import media
from ..core.context import Ctx
from ..core.schema import Script
from ..providers.audio import procedural

OUTPUTS = ["audio/mix.wav", "sound.json"]
log = logging.getLogger("factory.sound")
SR = media.SR


def _track_for(ctx: Ctx, mood: str, dur: float, rng: random.Random, used: set[str]) -> tuple[np.ndarray, str]:
    folder = ctx.cfg.path("sound.music_dir", "assets/music") / mood
    files = sorted([p for p in folder.glob("*") if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a", ".flac")]) \
        if folder.exists() else []
    fresh = [p for p in files if str(p) not in used] or files
    if fresh:
        path = rng.choice(fresh)
        used.add(str(path))
        pcm = media.load_audio(path)
        if len(pcm) < dur * SR:                         # короткий трек — зацикливаем
            pcm = np.tile(pcm, int(dur * SR / max(1, len(pcm))) + 1)
        start = rng.randint(0, max(0, len(pcm) - int(dur * SR))) if len(pcm) > dur * SR * 1.5 else 0
        return pcm[start:start + int(dur * SR)], path.name
    return procedural.music_pad(mood, dur, seed=rng.randint(0, 9999)), f"procedural:{mood}"


def _smooth_gate(env: np.ndarray, thr: float, attack: float, release: float) -> np.ndarray:
    """0..1 «голос звучит» с разными постоянными атаки и спада (как у компрессора)."""
    hop = 480
    frames = env[::hop] > thr
    a = 1 - np.exp(-hop / (attack * SR))
    r = 1 - np.exp(-hop / (release * SR))
    out = np.zeros(len(frames), dtype=np.float32)
    y = 0.0
    for i, on in enumerate(frames):
        target = 1.0 if on else 0.0
        y += (target - y) * (a if target > y else r)
        out[i] = y
    return np.repeat(out, hop)[: len(env)]


def _place(buf: np.ndarray, clip: np.ndarray, at: float, gain_db: float) -> None:
    i = int(max(0.0, at) * SR)
    if i >= len(buf):
        return
    j = min(len(buf), i + len(clip))
    buf[i:j] += clip[: j - i] * media.db_to_gain(gain_db)


def _ramp_mask(n: int, windows: list[tuple[float, float]], ramp: float = 0.15) -> np.ndarray:
    m = np.ones(n, dtype=np.float32)
    r = int(ramp * SR)
    for a, b in windows:
        i, j = int(a * SR), int(b * SR)
        m[max(0, i):min(n, j)] = 0
        if r:
            lo = max(0, i - r)
            m[lo:i] = np.minimum(m[lo:i], np.linspace(1, 0, i - lo)) if i > lo else m[lo:i]
            hi = min(n, j + r)
            m[j:hi] = np.minimum(m[j:hi], np.linspace(0, 1, hi - j)) if hi > j else m[j:hi]
    return m


def measure_lufs(path: Path) -> tuple[float, float]:
    """Интегральная громкость (LUFS) и true peak (dBTP) по EBU R128."""
    err = media.run_stderr(["ffmpeg", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"])
    tail = err[err.rfind("Summary:"):]
    i = float(re.search(r"I:\s+(-?[\d.]+) LUFS", tail).group(1))
    m = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", tail)
    tp = float(m.group(1)) if m and m.group(1) != "-inf" else -99.0
    return i, tp


def normalize_loudness(src: Path, dst: Path, lufs: float, tp: float) -> dict:
    """Точная громкость без перегруза: limiter с порогом (TP − нужный подъём), затем линейный подъём.

    loudnorm в линейном режиме не может поднять громкость, если упирается в пики, и
    молча переходит в динамический режим с недобором (−16 вместо −14). Поэтому сначала
    срезаем пики ровно настолько, насколько нужно, потом поднимаем уровень целиком.
    """
    i0, _ = measure_lufs(src)
    gain = lufs - i0
    cur = src
    passes: list[Path] = []
    for attempt in range(3):
        ceiling = min(1.0, max(0.0625, 10 ** ((tp - 0.3 - gain) / 20)))
        out = dst.with_suffix(f".pass{attempt}.wav")
        media.run(["ffmpeg", "-i", str(cur), "-af",
                   f"alimiter=limit={ceiling:.5f}:attack=3:release=60:level=disabled,volume={gain:.2f}dB",
                   "-ar", str(SR), str(out)])
        passes.append(out)
        cur = out
        i1, _ = measure_lufs(out)
        if abs(i1 - lufs) <= 0.4:
            break
        gain = (lufs - i1) * 1.35              # лимитер съедает часть подъёма — добираем с запасом
    cur.replace(dst)
    for p in passes:
        p.unlink(missing_ok=True)
    i_final, tp_final = measure_lufs(dst)
    return {"input_i": round(i0, 2), "output_i": round(i_final, 2), "output_tp": round(tp_final, 2)}


def run(ctx: Ctx) -> dict:
    cfg = ctx.cfg
    scfg = cfg.section("sound")
    sc = Script.model_validate(ctx.job.read_json("script.json"))
    tl = ctx.job.read_json("timeline.json")
    voice = media.load_audio(ctx.job.p("audio", "voice.wav"))
    n = len(voice)
    dur = n / SR
    rng = random.Random(ctx.job_id)
    blocks = {b["block_id"]: b for b in tl["blocks"]}
    order = [b.block_id for b in sc.beats]
    beats = {b.block_id: b for b in sc.beats}

    # ---------- музыка ----------
    ranges = []
    covered = set()
    for mr in sc.music_plan:
        i0, i1 = order.index(mr.from_block), order.index(mr.to_block)
        ids = order[i0:i1 + 1]
        covered |= set(ids)
        ranges.append((blocks[mr.from_block]["start"], blocks[mr.to_block]["end"], mr.mood, mr.duck_db))
    for bid in order:                                  # непокрытые блоки — нейтральная подложка
        if bid not in covered:
            ranges.append((blocks[bid]["start"], blocks[bid]["end"], "neutral", float(scfg.get("duck_db_default", -14))))
    ranges.sort()
    music = np.zeros(n, dtype=np.float32)
    duck_curve = np.full(n, float(scfg.get("duck_db_default", -14)), dtype=np.float32)
    xf = float(scfg.get("crossfade_s", 1.5))
    used_tracks: set[str] = set(ctx.db.kv_get("music:last_used", []) or [])
    tracks_log = []
    for a, b, mood, duck in ranges:
        a2, b2 = max(0.0, a - xf / 2), min(dur, b + xf / 2)
        pcm, name = _track_for(ctx, mood, b2 - a2, rng, used_tracks)
        k = min(len(pcm), int(xf * SR))
        env = np.ones(len(pcm), dtype=np.float32)
        if k:
            env[:k] = np.linspace(0, 1, k)
            env[-k:] = np.minimum(env[-k:], np.linspace(1, 0, k))
        i = int(a2 * SR)
        j = min(n, i + len(pcm))
        music[i:j] += (pcm * env)[: j - i]
        duck_curve[int(a * SR):int(b * SR)] = duck
        tracks_log.append({"from": round(a, 2), "to": round(b, 2), "mood": mood, "track": name})
    ctx.db.kv_set("music:last_used", sorted(used_tracks)[-30:])
    active = music[np.abs(music) > 1e-4]
    music_rms = float(np.sqrt(np.mean(active ** 2))) if len(active) else 1.0
    music = music / (music_rms or 1.0)                  # RMS = 0 dBFS, дальше задаём уровень явно

    # ---------- ducking (sidechain от голоса) ----------
    env = media.rms_envelope(voice, SR, 0.05)
    gate = _smooth_gate(env, thr=0.02, attack=float(scfg.get("duck_attack_s", 0.08)),
                        release=float(scfg.get("duck_release_s", 0.35)))
    gain_db = float(scfg.get("music_gain_db", -22)) + gate * duck_curve   # в паузах −22 dBFS RMS, под голосом ещё ниже
    music = music * np.power(10, gain_db / 20).astype(np.float32)

    # ---------- полная тишина перед кульминацией ----------
    silences = [(s["start"], s["end"]) for s in tl.get("planned_silences", [])]
    if silences:
        music *= _ramp_mask(n, silences)

    # ---------- SFX ----------
    sfx = np.zeros(n, dtype=np.float32)
    sfx_dir = cfg.path("sound.sfx_dir", "assets/generated/sfx")
    lib = {name: media.load_audio(sfx_dir / f"{name}.wav") for name in ("whoosh", "impact", "riser", "ambience")
           if (sfx_dir / f"{name}.wav").exists()}
    gains = scfg.get("sfx_gain_db", {})
    events = []

    def add(name: str, at: float, why: str, gain_offset: float = 0.0) -> None:
        if name in lib:
            _place(sfx, lib[name], at, float(gains.get(name, -18)) + gain_offset)
            events.append({"sfx": name, "at": round(at, 2), "why": why})

    prev_act = None
    for bid in order:
        bl, beat = blocks[bid], beats[bid]
        if prev_act and beat.act != prev_act and scfg.get("whoosh_on_act_change", True):
            add("whoosh", bl["start"] - 0.35, f"смена акта → {beat.act}")
        sil = next((s for s in tl.get("planned_silences", []) if s["block_id"] == bid), None)
        if beat.music_cue == "riser" or sil:
            end_at = sil["start"] if sil else bl["speech_start"]
            add("riser", end_at - float(scfg.get("riser_before_climax_s", 1.6)), f"нарастание перед {bid}")
        if beat.music_cue in ("impact", "silence_before", "drop"):
            add("impact", bl["speech_start"] - 0.05, f"{beat.music_cue} в {bid}")
        prev_act = beat.act
    tl_segs = {s["id"]: s for s in tl["segments"]}
    for seg in sc.segments:
        for name in seg.sfx:
            if name in lib and name != "ambience":
                add(name, tl_segs[seg.id]["start"] - (0.3 if name == "whoosh" else 0.0), f"сценарий {seg.id}", -2)
    shots = ctx.job.read_json("shots.json")["shots"] if ctx.job.exists("shots.json") else []
    for sh in shots:
        for c in sh.get("counters", []):
            add("impact", sh["start"] + c["t"], "счётчик", -6)
    if "ambience" in lib:
        amb_blocks = [bid for bid in order if beats[bid].visual_register in ("stock", "parallax", "archive")]
        for bid in amb_blocks:
            bl = blocks[bid]
            seglen = int((bl["end"] - bl["start"]) * SR)
            amb = np.tile(lib["ambience"], seglen // max(1, len(lib["ambience"])) + 1)[:seglen]
            _place(sfx, amb, bl["start"], float(gains.get("ambience", -34)))
    if silences:
        sfx *= _ramp_mask(n, silences, ramp=0.05)

    # ---------- сведение ----------
    mix = voice + music + sfx
    raw = media.save_wav(ctx.job.p("audio", "mix_raw.wav"), mix / max(1.0, float(np.max(np.abs(mix)))))
    media.save_wav(ctx.job.p("audio", "music_stem.wav"), music)
    m = normalize_loudness(raw, ctx.job.p("audio", "mix.wav"), float(scfg.get("final_lufs", -14)),
                           float(scfg.get("final_true_peak", -1.5)))
    report = {"music": tracks_log, "sfx": events, "silences": silences, "loudness": m}
    ctx.job.write_json("sound.json", report)
    return {"tracks": len(tracks_log), "sfx": len(events), "silences": len(silences),
            "procedural_music": sum(1 for t in tracks_log if t["track"].startswith("procedural"))}
