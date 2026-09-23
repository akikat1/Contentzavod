"""s02 — исследование: Википедия (бесплатно, без ключа) + LLM выписывает факты с источниками."""
from __future__ import annotations

import logging
import urllib.parse

import httpx

from ..core.context import Ctx
from ..core.schema import Research
from ..providers.llm import fixture as fx

OUTPUTS = ["research.json"]
log = logging.getLogger("factory.research")
UA = {"User-Agent": "contentzavod/0.1 (https://github.com/akikat1/Contentzavod)"}


def wiki_materials(topic: str, langs: list[str], per_lang: int = 3, max_chars: int = 9000) -> list[dict]:
    out: list[dict] = []
    for lang in langs:
        api = f"https://{lang}.wikipedia.org/w/api.php"
        try:
            r = httpx.get(api, params={"action": "query", "list": "search", "srsearch": topic, "format": "json",
                                       "srlimit": per_lang}, headers=UA, timeout=20)
            r.raise_for_status()
            titles = [h["title"] for h in r.json().get("query", {}).get("search", [])]
            for title in titles:
                e = httpx.get(api, params={"action": "query", "prop": "extracts", "explaintext": 1, "titles": title,
                                           "format": "json", "redirects": 1}, headers=UA, timeout=30)
                e.raise_for_status()
                pages = e.json().get("query", {}).get("pages", {})
                text = next(iter(pages.values()), {}).get("extract", "")
                if len(text) > 400:
                    out.append({"title": title, "lang": lang, "text": text[:max_chars],
                                "url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"})
        except httpx.HTTPError as ex:
            log.warning("Википедия (%s) недоступна: %s", lang, ex)
    return out


def run(ctx: Ctx) -> dict:
    topic = ctx.job.read_json("topic.json")
    langs = [ctx.cfg.language] + (["en"] if ctx.cfg.language != "en" else [])
    materials = [] if ctx.cfg.offline else wiki_materials(topic["topic"], langs)
    sources = [{"id": f"r{i + 1:02d}", "url": m["url"], "title": m["title"], "claim": ""}
               for i, m in enumerate(materials)]
    mat_text = "\n\n".join(f"[{s['id']}] {m['title']} ({m['lang']})\n{m['text']}" for s, m in
                           zip(sources, materials, strict=True)) or "Материалов не найдено — опирайся на общеизвестные факты и укажи источник r00 (энциклопедические знания модели)."
    if not sources:
        sources = [{"id": "r00", "url": "", "title": "Энциклопедические знания модели (требует проверки)", "claim": ""}]
    user = ctx.prompt("research", topic=topic["topic"], angle=topic.get("angle") or "найди сам",
                      materials=mat_text, sources="\n".join(f"{s['id']}: {s['title']} {s['url']}" for s in sources))

    def prepare(data: dict) -> dict:
        data["sources"] = sources                      # источники — только найденные, модель их не выдумывает
        return data

    res = ctx.llm.json("research", ctx.system_prompt(), user, Research, prepare=prepare, max_tokens=8000,
                       temperature=0.3, fixture=lambda: fx.research(topic["topic"], topic.get("angle", ""), sources))
    ctx.job.write_json("research.json", res)
    return {"facts": len(res.facts), "sources": len(res.sources), "materials_chars": len(mat_text)}
