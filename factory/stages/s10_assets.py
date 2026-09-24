"""s10 — ассеты для планов: сток, изображения, карты глубины для параллакса.

Match cut: для первого плана блока из нескольких стоковых кандидатов берём тот,
чей средний цвет ближе к последнему кадру предыдущего блока. Неудачный сток
падает на изображение, изображение — на процедурный фон: кадр есть всегда.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from ..core import media
from ..core.context import Ctx
from ..core.gpu_lock import gpu_session
from ..providers.image.providers import ImageRouter, ImageUnavailable
from ..providers.video.stock import StockRouter, StockUnavailable

OUTPUTS = ["assets.json"]
log = logging.getLogger("factory.assets")


def _avg_color(path: Path) -> np.ndarray | None:
    try:
        if path.suffix.lower() in (".mp4", ".mov", ".webm"):
            raw = media.run(["ffmpeg", "-ss", "0.5", "-i", str(path), "-frames:v", "1", "-vf", "scale=16:9",
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]).stdout
            return np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).mean(axis=0)
        return np.asarray(Image.open(path).convert("RGB").resize((16, 9))).reshape(-1, 3).mean(axis=0)
    except Exception:  # noqa: BLE001
        return None


def _thumb_color(url: str) -> np.ndarray | None:
    try:
        r = httpx.get(url, timeout=15)
        from io import BytesIO  # noqa: PLC0415
        return np.asarray(Image.open(BytesIO(r.content)).convert("RGB").resize((16, 9))).reshape(-1, 3).mean(axis=0)
    except Exception:  # noqa: BLE001
        return None


def _depth_maps(ctx: Ctx, items: list[tuple[str, Path]]) -> dict[str, str]:
    """MiDaS-small на GPU под мьютексом; без torch — эвристическая глубина прямо в рендере."""
    mode = ctx.cfg.get("visual.parallax.depth", "auto")
    if mode == "heuristic" or not items:
        return {}
    try:
        import torch  # noqa: PLC0415
    except ImportError:
        return {}
    out: dict[str, str] = {}
    holder: dict = {}

    def unload() -> None:
        holder.clear()

    try:
        with gpu_session(ctx.cfg.path("paths.gpu_lock", "data/gpu.lock"), "parallax-depth", unload):
            model = torch.hub.load("intel-isl/MiDaS", "MiDaS_small", trust_repo=True)
            transform = torch.hub.load("intel-isl/MiDaS", "transforms", trust_repo=True).small_transform
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            model.to(dev).eval()
            holder["m"] = model
            for shot_id, img_path in items:
                img = np.asarray(Image.open(img_path).convert("RGB"))
                with torch.no_grad():
                    pred = model(transform(img).to(dev))
                    pred = torch.nn.functional.interpolate(pred.unsqueeze(1), size=img.shape[:2], mode="bicubic",
                                                           align_corners=False).squeeze().cpu().numpy()
                pred = (pred - pred.min()) / (np.ptp(pred) or 1)
                dp = ctx.job.p("assets", f"{shot_id}_depth.png")
                Image.fromarray((pred * 255).astype(np.uint8)).save(dp)
                out[shot_id] = str(dp)
    except Exception as e:  # noqa: BLE001 — глубина — улучшение, не требование
        log.warning("MiDaS недоступен (%s) — параллакс на эвристической глубине", e)
    return out


def run(ctx: Ctx) -> dict:
    data = ctx.job.read_json("shots.json")
    w, h = data["width"], data["height"]
    images = ImageRouter(ctx.cfg, ctx.pool)
    stock = StockRouter(ctx.cfg)
    suffix = ctx.cfg.get("visual.image_style_suffix", "")
    topic = ctx.job.read_json("topic.json")["topic"]
    assets: dict[str, dict] = {}
    last_color: np.ndarray | None = None
    prev_block = None
    fallbacks = 0
    for shot in data["shots"]:
        kind, vis = shot["kind"], shot["visual"]
        rec: dict = {"kind": kind}
        if kind == "stock":
            try:
                cands = stock.candidates(vis["query"], "landscape" if w >= h else "portrait",
                                         min_duration=min(8.0, shot["end"] - shot["start"]))
                pick = cands[shot["variant"] % len(cands)]
                if ctx.cfg.get("visual.match_cut", True) and shot["block_id"] != prev_block and last_color is not None:
                    scored = [(c, _thumb_color(c.thumb)) for c in cands[:5] if c.thumb]
                    scored = [(c, col) for c, col in scored if col is not None]
                    if scored:
                        pick = min(scored, key=lambda x: float(np.linalg.norm(x[1] - last_color)))[0]
                path = stock.download(pick)
                dur = media.duration(path)
                need = shot["end"] - shot["start"]
                offset = max(0.0, min(dur - need, dur * (0.1 + 0.2 * ((shot["variant"] * 37) % 10) / 10)))
                rec.update(asset=str(path), provider=pick.provider, stock_offset=round(offset, 2))
            except (StockUnavailable, httpx.HTTPError, media.FFmpegError) as e:
                log.info("План %s: сток недоступен (%s) — генерирую изображение", shot["id"], e)
                kind = "image"
                shot["kind"] = "image"
                vis = {"marker": "IMAGE", "prompt": f"{vis.get('query', topic)}, documentary footage still"}
                shot["visual"] = vis
                fallbacks += 1
        if kind in ("image", "parallax"):
            prompt = vis.get("prompt") or vis.get("query") or topic
            if shot["register"] == "archive":
                prompt += ", black and white archival photograph, early 20th century, film grain"
            try:
                path, prov = images.get(f"{prompt}, {suffix}", shot["variant"], w, h)
            except ImageUnavailable as e:
                raise RuntimeError(f"План {shot['id']}: нет изображения ни у одного провайдера: {e}") from e
            rec.update(asset=str(path), provider=prov)
        if rec.get("asset"):
            c = _avg_color(Path(rec["asset"]))
            if c is not None:
                last_color = c
        assets[shot["id"]] = rec
        prev_block = shot["block_id"]
    depth = _depth_maps(ctx, [(sid, Path(r["asset"])) for sid, r in assets.items()
                              if r["kind"] == "parallax" and r.get("asset")])
    for sid, dp in depth.items():
        assets[sid]["depth"] = dp
    ctx.job.write_json("shots.json", data)          # сток мог замениться на изображение
    ctx.job.write_json("assets.json", assets)
    providers: dict[str, int] = {}
    for r in assets.values():
        if r.get("provider"):
            providers[r["provider"]] = providers.get(r["provider"], 0) + 1
    if providers.get("procedural") and not ctx.cfg.offline:
        ctx.notes.append(f"{providers['procedural']} планов на процедурном фоне — проверьте ключи Gemini/Pollinations")
    return {"assets": len(assets), "providers": providers, "stock_to_image": fallbacks, "depth_maps": len(depth)}
