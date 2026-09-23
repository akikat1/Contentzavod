"""s12 — посценный рендер мастера 16:9 и склейка без перекодирования.

Каждый план — отдельный ffmpeg (параллельно не больше render.parallel_scenes:
на 16 ГБ ОЗУ больше двух не нужно). Уже отрендеренные сцены с тем же описанием
не перерендериваются — повторный прогон после ремонта дешёвый.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..core import media
from ..core.context import Ctx
from ..core.encoder import describe, video_codec_args
from ..render.scene import Frame, ShotSpec, render

OUTPUTS = ["out/master.mp4"]
log = logging.getLogger("factory.render")


def shot_spec(shot: dict, asset: dict, frames: int) -> ShotSpec:
    vis = shot["visual"]
    return ShotSpec(
        id=shot["id"], kind=shot["kind"], frames=frames, asset=asset.get("asset"), depth=asset.get("depth"),
        variant=shot["variant"], stock_offset=float(asset.get("stock_offset", 0.0)),
        text=shot.get("text") or vis.get("content", ""), emph_words=shot.get("emph_words", []),
        value=vis.get("value"), prefix=vis.get("prefix") or "", suffix=vis.get("suffix") or "",
        label=vis.get("label") or "", title=vis.get("title") or "", data=vis.get("data") or [],
        grade=shot.get("grade", {}), punches=shot.get("punches", []), counters=shot.get("counters", []),
        fade_in=float(shot.get("fade_in", 0.0)))


def render_shots(specs: list[ShotSpec], fr: Frame, out_dir: Path, parallel: int, punch: dict) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    def one(spec: ShotSpec) -> tuple[str, Path]:
        key = hashlib.sha256(json.dumps([spec.__dict__, fr.w, fr.h, fr.fps, fr.codec], default=str,
                                        sort_keys=True).encode()).hexdigest()[:16]
        path = out_dir / f"{spec.id}.mp4"
        stamp = out_dir / f"{spec.id}.key"
        if path.exists() and stamp.exists() and stamp.read_text() == key:
            return spec.id, path
        render(spec, fr, path, float(punch.get("scale", 1.08)) - 1, float(punch.get("dur_s", 0.3)))
        stamp.write_text(key)
        return spec.id, path

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        futs = [ex.submit(one, s) for s in specs]
        for f in as_completed(futs):
            sid, p = f.result()
            paths[sid] = p
    return [paths[s.id] for s in specs]


def concat(paths: list[Path], out: Path) -> Path:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in paths), encoding="utf-8")
    media.run(["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    return out


def mux(video: Path, audio: Path, out: Path, duration: float, bitrate: str, subs: Path | None = None,
        codec: list[str] | None = None) -> Path:
    args = ["ffmpeg", "-i", str(video), "-i", str(audio)]
    if subs:
        args += ["-vf", f"ass='{subs}'", *(codec or []), ]
    else:
        args += ["-c:v", "copy"]
    args += ["-c:a", "aac", "-b:a", bitrate, "-ac", "2", "-ar", "48000", "-map", "0:v:0", "-map", "1:a:0",
             "-t", f"{duration:.3f}", "-movflags", "+faststart", str(out)]
    media.run(args)
    return out


def run(ctx: Ctx) -> dict:
    cfg = ctx.cfg
    data = ctx.job.read_json("shots.json")
    assets = ctx.job.read_json("assets.json")
    timeline = ctx.job.read_json("timeline.json")
    fps = data["fps"]
    mode, quality = cfg.get("render.encoder", "auto"), cfg.get("render.quality", "normal")
    fr = Frame(data["width"], data["height"], fps, video_codec_args(mode, quality, fps))
    specs = [shot_spec(s, assets.get(s["id"], {}), s["frames"]) for s in data["shots"]]
    paths = render_shots(specs, fr, ctx.job.p("scenes"), int(cfg.get("render.parallel_scenes", 2)),
                         cfg.section("visual.zoom_punch"))
    video = concat(paths, ctx.job.p("scenes", "video.mp4"))
    subs = ctx.job.p("subs", "master.ass") if cfg.get("subtitles.master_burn", False) else None
    master = mux(video, ctx.job.p("audio", "mix.wav"), ctx.job.p("out", "master.mp4"), timeline["duration"],
                 cfg.get("render.audio_bitrate", "192k"), subs, fr.codec)
    shutil.copyfile(ctx.job.p("subs", "master.srt"), ctx.job.p("out", "master.srt"))
    info = media.probe(master)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    return {"encoder": describe(mode), "shots": len(specs), "duration_s": round(float(info["format"]["duration"]), 2),
            "resolution": f"{v['width']}x{v['height']}", "size_mb": round(master.stat().st_size / 2**20, 1)}
