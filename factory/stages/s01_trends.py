"""s01 — выбор темы и формата.

Тема: задана вручную → берём её; иначе кандидаты из ниши, Википедии «В этот
день» и Google Trends, а выбирает LLM с учётом уже вышедших тем.
Формат: ротация (не чаще раза в N выпусков) с учётом предложения LLM.
"""
from __future__ import annotations

import datetime as dt
import logging
import xml.etree.ElementTree as ET

import httpx

from ..core.context import Ctx
from ..core.schema import CONTENT_TYPES, TopicPick
from ..engagement.format_rotation import choose_format
from ..providers.llm import fixture as fx

OUTPUTS = ["topic.json"]
log = logging.getLogger("factory.trends")


def _wikipedia_onthisday(lang: str) -> list[str]:
    d = dt.date.today()
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/{lang}/onthisday/events/{d:%m}/{d:%d}"
    r = httpx.get(url, timeout=20, headers={"User-Agent": "contentzavod/0.1"})
    r.raise_for_status()
    events = r.json().get("events", [])
    return [f"{e.get('year')}: {e.get('text')}" for e in events[:8] if e.get("text")]


def _google_trends(geo: str) -> list[str]:
    r = httpx.get(f"https://trends.google.com/trending/rss?geo={geo}", timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    return [i.findtext("title") for i in root.iter("item") if i.findtext("title")][:10]


def run(ctx: Ctx) -> dict:
    meta = ctx.meta()
    recent = [r["topic"] for r in ctx.db.all(
        "SELECT topic FROM jobs WHERE job_id != ? AND topic != '' ORDER BY created_at DESC LIMIT 30", (ctx.job_id,))]
    if meta.get("topic_given"):
        pick = TopicPick(topic=meta["topic_given"], angle="", content_type=meta.get("format_given") or "explainer",
                         why="тема задана вручную")
        suggested = meta.get("format_given")
    else:
        cands: list[str] = list(ctx.niche.get("seed_topics", []))
        sources = ctx.cfg.get("trends.sources", [])
        if not ctx.cfg.offline:
            for name, fn in (("wikipedia_onthisday", lambda: _wikipedia_onthisday(ctx.cfg.language)),
                             ("google_trends_rss", lambda: _google_trends(ctx.cfg.get("trends.google_trends_geo",
                                                                                      "RU")))):
                if name in sources:
                    try:
                        cands += fn()
                    except (httpx.HTTPError, ET.ParseError) as e:
                        log.warning("Источник трендов %s недоступен: %s", name, e)
        cands = cands[: int(ctx.cfg.get("trends.candidates", 12)) + len(ctx.niche.get("seed_topics", []))]
        user = ctx.prompt("trends", candidates="\n".join(f"- {c}" for c in cands),
                          recent_topics="\n".join(f"- {t}" for t in recent) or "—",
                          avoid=", ".join(ctx.niche.get("avoid", [])) or "—",
                          content_types=", ".join(CONTENT_TYPES))
        pick = ctx.llm.json("research", ctx.system_prompt(), user, TopicPick, max_tokens=1500, temperature=0.9,
                            fixture=lambda: fx.topic_pick(ctx.niche.get("seed_topics", ["Тема"]), recent,
                                                          "investigation"))
        suggested = pick.content_type
    content_type, reason = choose_format(ctx.db, ctx.cfg, suggested=suggested, seed=ctx.job_id)
    if meta.get("format_given") in CONTENT_TYPES:
        content_type, reason = meta["format_given"], "формат задан вручную"
    data = {"topic": pick.topic, "angle": pick.angle, "content_type": content_type, "why": pick.why,
            "format_reason": reason}
    ctx.job.write_json("topic.json", data)
    ctx.db.execute("UPDATE jobs SET topic=?, content_type=? WHERE job_id=?", (pick.topic, content_type, ctx.job_id))
    return data
