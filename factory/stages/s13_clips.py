"""s13 — шортсы как самостоятельный продукт, а не вырезанный кусок.

Для каждого кандидата из beat sheet: свой диапазон (по границам предложений,
15–58 с), планы перерендериваются в 9:16 из тех же ассетов (настоящая
вертикальная перекомпоновка, а не кроп мастера), хук-плашка в первые 1.6 с,
финальный крючок, вшитые karaoke-субтитры над safe-zone интерфейса площадок.
"""
from __future__ import annotations

import logging

from ..core import media
from ..core.context import Ctx
from ..core.encoder import video_codec_args
from ..core.schema import Script
from ..render.scene import Frame
from ..render.subtitles import all_words, write_ass
from .s12_render import concat, render_shots, shot_spec

OUTPUTS = ["shorts/shorts.json"]
log = logging.getLogger("factory.clips")


def pick_range(tl: dict, block_ids: list[str], min_s: float, max_s: float) -> tuple[float, float] | None:
    segs = [s for s in tl["segments"] if s["block_id"] in block_ids]
    if not segs:
        return None
    start = max(0.0, segs[0]["start"] - 0.15)
    end = segs[-1]["end"] + 0.35
    if end - start > max_s:                      # режем по концу предложения, а не посреди слова
        limit = start + max_s
        ends = [w["end"] for s in segs for w in s["words"]
                if w["w"].rstrip("»\"").endswith((".", "!", "?", "…")) and w["end"] <= limit - 0.3]
        if not ends:
            return None
        end = max(ends) + 0.35
    if end - start < min_s:
        return None
    return start, min(end, tl["duration"])


def run(ctx: Ctx) -> dict:
    cfg = ctx.cfg
    scfg = cfg.section("shorts")
    out_dir = ctx.job.p("shorts")
    if not scfg.get("enabled", True):
        ctx.job.write_json("shorts/shorts.json", [])
        return {"shorts": 0}
    sc = Script.model_validate(ctx.job.read_json("script.json"))
    tl = ctx.job.read_json("timeline.json")
    data = ctx.job.read_json("shots.json")
    assets = ctx.job.read_json("assets.json")
    fps = data["fps"]
    W, H = int(scfg.get("width", 1080)), int(scfg.get("height", 1920))
    fr = Frame(W, H, fps, video_codec_args(cfg.get("render.encoder", "auto"), cfg.get("render.quality", "normal"), fps))
    sub = cfg.section("subtitles")
    made = []
    for idx, cand in enumerate(sc.shorts_candidates[: int(scfg.get("max_count", 4))], 1):
        rng = pick_range(tl, cand.block_ids, float(scfg.get("min_s", 15)), float(scfg.get("max_s", 58)))
        if rng is None:
            log.info("Кандидат %s не укладывается в 15–58 с — пропуск", cand.block_ids)
            continue
        a, b = rng
        f_a, f_b = round(a * fps), round(b * fps)
        specs = []
        for sh in data["shots"]:
            lo, hi = max(sh["f0"], f_a), min(sh["f1"], f_b)
            if hi <= lo:
                continue
            clip = dict(sh)
            clip["id"] = f"v{idx:02d}_{sh['id']}"
            off = (lo - sh["f0"]) / fps
            clip["punches"] = [p - off for p in sh.get("punches", []) if 0 <= p - off < (hi - lo) / fps - 0.3]
            clip["counters"] = [{**c, "t": c["t"] - off} for c in sh.get("counters", [])
                                if 0 <= c["t"] - off < (hi - lo) / fps - 0.8]
            clip["fade_in"] = 0.0 if not specs else sh.get("fade_in", 0.0)
            asset = dict(assets.get(sh["id"], {}))
            if asset.get("stock_offset") is not None:
                asset["stock_offset"] = float(asset["stock_offset"]) + off
            specs.append(shot_spec(clip, asset, hi - lo))
        dur = (f_b - f_a) / fps
        hook_s, outro_s = float(scfg.get("hook_card_s", 1.6)), float(scfg.get("outro_card_s", 2.0))
        specs[0].overlay_text.append({"text": cand.hook, "t0": 0, "t1": hook_s, "pos": "upper", "scale": 0.075})
        outro = cand.outro_hook or scfg.get("outro_text", "")
        if outro:
            last, t_last = specs[-1], specs[-1].frames / fps
            last.overlay_text.append({"text": outro, "t0": max(0.0, t_last - outro_s), "t1": t_last, "pos": "upper",
                                      "scale": 0.065})
        paths = render_shots(specs, fr, ctx.job.p("scenes", "vertical"), int(cfg.get("render.parallel_scenes", 2)),
                             cfg.section("visual.zoom_punch"))
        video = concat(paths, ctx.job.p("scenes", "vertical", f"short_{idx:02d}_video.mp4"))
        words = all_words(tl, a, b)
        ass = write_ass(words, ctx.job.p("subs", f"short_{idx:02d}.ass"), W, H, font=sub.get("font", "DejaVu Sans"),
                        max_words=3, base_color=sub.get("base_color", "&H00FFFFFF"),
                        emph_color=sub.get("emph_color", "&H0000D7FF"), outline=int(sub.get("outline", 4)) + 1,
                        margin_v=int(H * float(scfg.get("safe_bottom_frac", 0.22))), karaoke=True,
                        active_color=sub.get("active_color", "&H0050FF50"))
        audio = ctx.job.p("audio", f"short_{idx:02d}.wav")
        media.run(["ffmpeg", "-ss", f"{a:.3f}", "-t", f"{dur:.3f}", "-i", str(ctx.job.p("audio", "mix.wav")),
                   "-af", f"afade=t=in:d=0.12,afade=t=out:st={max(0, dur - 0.3):.3f}:d=0.3", str(audio)])
        out = out_dir / f"short_{idx:02d}.mp4"
        vf = f"ass='{ass}'" if cfg.get("subtitles.shorts_burn", True) else "null"
        media.run(["ffmpeg", "-i", str(video), "-i", str(audio), "-vf", vf, *fr.codec, "-c:a", "aac", "-b:a", "160k",
                   "-ac", "2", "-map", "0:v:0", "-map", "1:a:0", "-t", f"{dur:.3f}", "-movflags", "+faststart",
                   str(out)])
        made.append({"file": str(out.relative_to(ctx.job.root)), "start": round(a, 2), "end": round(b, 2),
                     "duration": round(dur, 2), "block_ids": cand.block_ids, "hook": cand.hook})
    ctx.job.write_json("shorts/shorts.json", made)
    return {"shorts": len(made), "durations": [m["duration"] for m in made]}
