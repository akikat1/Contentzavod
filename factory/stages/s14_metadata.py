"""s14 — упаковка: заголовок (A/B по истории CTR), описания под каждую площадку,
главы YouTube по реальному таймлайну, источники, раскрытие ИИ, обложка."""
from __future__ import annotations

from ..core.context import Ctx
from ..core.schema import Metadata, Script
from ..engagement.hook_bank import best_title_style, record_hook
from ..providers.llm import fixture as fx
from ..render.thumbnail import frame_from, make_thumbnail

OUTPUTS = ["metadata.json", "out/thumbnail.jpg"]
PLATFORMS = ["youtube", "telegram", "vk", "rutube", "bluesky", "tiktok", "instagram", "facebook", "postiz"]


def _ts(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def youtube_chapters(sc: Script, tl: dict) -> list[tuple[float, str]]:
    """YouTube требует: первая глава 00:00, минимум 3 главы, каждая ≥ 10 с."""
    starts = {b["block_id"]: b["start"] for b in tl["blocks"]}
    raw = sorted((starts[c.block_id], c.title) for c in sc.chapters if c.block_id in starts)
    if not raw:
        return []
    raw[0] = (0.0, raw[0][1])
    out: list[tuple[float, str]] = []
    for t, title in raw:
        if out and t - out[-1][0] < 10:
            continue
        out.append((t, title))
    if out and tl["duration"] - out[-1][0] < 10:
        out.pop()
    return out if len(out) >= 3 else []


def run(ctx: Ctx) -> dict:
    sc = Script.model_validate(ctx.job.read_json("script.json"))
    tl = ctx.job.read_json("timeline.json")
    sheet = ctx.job.read_json("beatsheet.json")
    styles = [t.style for t in sc.title_candidates]
    preferred = best_title_style(ctx.db, styles, seed=hash(ctx.job_id) % 10**6) or styles[0]
    chapters = youtube_chapters(sc, tl)
    user = ctx.prompt("metadata", topic=sc.topic,
                      title_candidates=[t.model_dump() for t in sc.title_candidates], preferred_style=preferred,
                      summary=sheet.get("angle", "") + ". " + sheet.get("hook", ""),
                      chapters="\n".join(f"{_ts(t)} {title}" for t, title in chapters) or "—")

    def prepare(data: dict) -> dict:
        plats = data.get("platforms") or {}
        base = plats.get("youtube") or next(iter(plats.values()), None)
        for p in PLATFORMS:                     # недостающие площадки — из YouTube-версии
            if p not in plats and base:
                plats[p] = base
        data["platforms"] = plats
        return data

    meta = ctx.llm.json("metadata", ctx.system_prompt(), user, Metadata, prepare=prepare, max_tokens=5000,
                        temperature=0.7, fixture=lambda: fx.metadata(sc, PLATFORMS))
    # детерминированные добавки: главы, источники, раскрытие ИИ — модель их не пишет
    sources = [s for s in sc.sources if s.url]
    tail = ""
    if chapters:
        tail += "\n\nГлавы:\n" + "\n".join(f"{_ts(t)} {title}" for t, title in chapters)
    if sources:
        tail += "\n\nИсточники:\n" + "\n".join(f"• {s.title}: {s.url}" for s in sources[:12])
    disclosure = ctx.cfg.get("channel.ai_disclosure", "")
    if disclosure:
        tail += f"\n\n{disclosure}"
    for name, pm in meta.platforms.items():
        if name in ("youtube", "vk", "rutube"):
            pm.description = (pm.description.strip() + tail)[:4900]
        elif disclosure and name in ("telegram", "facebook"):
            pm.description = (pm.description.strip() + "\n\n" + disclosure)[:1000]
        pm.title = pm.title[:100]
    meta.platforms["youtube"].title = meta.chosen_title[:100]
    ctx.job.write_json("metadata.json", meta)

    # обложка: план с максимальным напряжением, у которого есть картинка/видео
    shots = ctx.job.read_json("shots.json")["shots"]
    assets = ctx.job.read_json("assets.json")
    tension = {b.block_id: b.tension for b in sc.beats}
    cands = sorted((s for s in shots if assets.get(s["id"], {}).get("asset")), key=lambda s: -tension[s["block_id"]])
    bg = frame_from(assets[cands[0]["id"]]["asset"]) if cands else None
    if bg is None:
        from PIL import Image  # noqa: PLC0415
        bg = Image.new("RGB", (1280, 720), (20, 24, 40))
    make_thumbnail(bg, meta.thumbnail_text, ctx.job.p("out", "thumbnail.jpg"))
    co = next((b for b in tl["blocks"] if b["block_id"] == sc.beats[0].block_id), None)
    record_hook(ctx.db, ctx.job_id, sc.content_type, sheet.get("hook", ""), meta.chosen_title, meta.title_style,
                (co["end"] - co["start"]) if co else 0.0)
    return {"title": meta.chosen_title, "style": meta.title_style, "chapters": len(chapters),
            "sources": len(sources)}
