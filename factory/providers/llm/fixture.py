"""Офлайн-фикстура LLM.

Детерминированно строит валидные объекты контракта (тема, исследование, beat sheet,
сценарий, метаданные) без сети. Нужна для сквозного прогона на машине без
интернета и для тестов. Текст шаблонный и помечается как синтетический:
джоб с фикстурным контентом никогда не публикуется на реальные площадки.
"""
from __future__ import annotations

import math
import urllib.parse
from typing import Any

from ...core.schema import BeatSheet, Script
from ...engagement.beatsheet_rules import validate_beats

ALT_REGISTERS = ["stock", "image", "text", "diagram", "counter", "parallax", "archive"]
REPEATABLE = {"rising", "explanation", "list_item", "context", "myth", "debunk"}

OPENERS = {
    "cold_open": "[emph]{topic}[/emph]. [pause:400] Звучит как обычная история, но в ней есть деталь, которую почти "
                 "никто не замечает.",
    "promise": "И вот главный вопрос этого ролика: [emph]почему[/emph] всё сложилось именно так? Ответ будет в конце, "
               "и он проще, чем кажется.",
    "context": "Чтобы понять, что произошло, вернёмся немного назад и посмотрим, с чего всё начиналось.",
    "rising": "Сначала всё выглядело логично. [breath] Но чем внимательнее смотришь на детали, тем больше вопросов.",
    "list_item": "Следующий пункт многих удивляет. Смотрите, в чём тут подвох.",
    "myth": "Многие уверены, что всё объясняется просто. Это звучит убедительно, правда?",
    "debunk": "А теперь посмотрим на факты. Они говорят совсем другое.",
    "twist": "И вот тут [emph]{number}[/emph]. [pause:350] Эта цифра переворачивает всю картину.",
    "reveal": "Документы показали то, чего никто не ожидал.",
    "explanation": "Разберём механизм по шагам, без сложных слов.",
    "climax": "[pause:700] [slow]И вот ответ.[/slow] Всё решила одна деталь, которую пропустили все.",
    "resolution": "После этого многое изменилось. Последствия видны до сих пор.",
    "finale": "Помните, с чего мы начали? {topic_short} — теперь вы знаете, почему. А как думаете вы? Напишите в "
              "комментариях.",
}
FILLERS = [
    "Смотрите, здесь важна последовательность событий.",
    "Каждый шаг по отдельности выглядел разумным.",
    "Но вместе они сложились в картину, которую никто не планировал.",
    "И вот тут начинается самое интересное.",
    "Давайте посмотрим внимательнее на эту деталь.",
    "Современники видели это совсем иначе, чем мы сегодня.",
    "Цифры здесь говорят сами за себя.",
    "Это легко пропустить, если не знать, куда смотреть.",
]
STOCK_Q = {"stock": "old city street", "map": "world map closeup"}
IMG_P = "documentary photo related to {topic_en}, dramatic natural light"


def _target_chars(seconds: float, cps: float) -> int:
    return int(seconds * cps)


def _fill(text: str, chars: int, seed: int) -> str:
    out, i = text, seed
    while len(out) < chars - 20:
        out += " " + FILLERS[i % len(FILLERS)]
        i += 1
    return out


# ---------------------------------------------------------------------- trends / research

def topic_pick(seed_topics: list[str], recent: list[str], content_type: str) -> dict:
    for t in seed_topics:
        if t not in recent:
            return {"topic": t, "angle": "деталь, которую обычно упускают", "content_type": content_type,
                    "why": "офлайн-фикстура: первая неиспользованная тема ниши"}
    return {"topic": seed_topics[0], "angle": "новый угол", "content_type": content_type, "why": "фикстура"}


def research(topic: str, angle: str, sources: list[dict] | None = None) -> dict:
    url = "https://ru.wikipedia.org/w/index.php?search=" + urllib.parse.quote(topic)
    sources = sources or [{"id": "r01", "url": url, "title": f"Википедия: {topic}", "claim": ""}]
    sid = sources[0]["id"]
    facts = []
    nums = ["1858", "1912", "2224", "46", "3", "1500", "9", "70", "12", "400", "18", "2"]
    for i, n in enumerate(nums, 1):
        facts.append({"id": f"f{i:02d}", "claim": f"Ключевой факт №{i} по теме «{topic}» связан с числом {n}.",
                      "source_id": sid, "numbers": [n]})
    return {"topic": topic, "angle": angle, "summary": f"Офлайн-сводка по теме «{topic}».",
            "facts": facts, "sources": sources,
            "surprising": ["число 2224 оказалось важнее, чем думали"], "open_questions": ["почему так вышло?"]}


# ---------------------------------------------------------------------- beat sheet

def beatsheet(topic: str, content_type: str, skeleton: list[dict], target_total: float, rules: dict) -> dict:
    max_beat = float(rules.get("max_beat_s", 40))
    cold_d = min(8.0, float(rules.get("cold_open_max_s", 10)) - 1)
    fin_d = min(18.0, max(6.0, target_total * 0.06))
    body = [dict(b) for b in skeleton[1:-1]]
    remaining = target_total - cold_d - fin_d
    n = max(len(body), math.ceil(remaining / (max_beat * 0.8)))
    climax_pos = next((i for i, b in enumerate(body) if b["act"] == "climax"), len(body) - 1)
    k = 0
    while len(body) < n:
        src = [b for b in body[:climax_pos] if b["act"] in REPEATABLE] or body[:1]
        copy = dict(src[k % len(src)])
        copy["tension"] = max(0.3, min(0.7, copy["tension"] - 0.05))
        body.insert(climax_pos, copy)
        climax_pos += 1
        k += 1
    seq = [dict(skeleton[0])] + body + [dict(skeleton[-1])]
    # соседние регистры различаются
    for i in range(1, len(seq)):
        if seq[i]["register"] == seq[i - 1]["register"]:
            nxt = seq[i + 1]["register"] if i + 1 < len(seq) else None
            seq[i]["register"] = next(r for r in ALT_REGISTERS if r not in (seq[i - 1]["register"], nxt))
    per = remaining / len(body)
    beats = []
    for i, b in enumerate(seq):
        d = cold_d if i == 0 else fin_d if i == len(seq) - 1 else per
        beats.append({
            "block_id": f"b{i + 1:02d}", "act": b["act"], "intent": b.get("note") or b["act"],
            "target_duration_s": round(d, 1), "tension": b["tension"], "visual_register": b["register"],
            "music_cue": b.get("cue", "none"), "opens_loop": None, "closes_loop": None, "callback_to": None,
        })
    beats[1]["opens_loop"] = "main_q"
    beats[-1]["callback_to"] = "b01"
    sheet = {"topic": topic, "content_type": content_type, "angle": "фикстура", "hook": topic,
             "target_duration_s": target_total, "beats": beats}
    # закрываем петлю на первом блоке от кульминации, который проходит проверку
    climax = max(i for i, b in enumerate(beats) if b["act"] == "climax") if any(
        b["act"] == "climax" for b in beats) else len(beats) - 2
    for j in range(climax, len(beats) - 1):
        for b in beats:
            b["closes_loop"] = None
        beats[j]["closes_loop"] = "main_q"
        probs = validate_beats(BeatSheet.model_validate(sheet).beats, rules, target_total)
        if not any("петля" in p for p in probs):
            break
    return sheet


# ---------------------------------------------------------------------- script

def script(job_id: str, topic: str, content_type: str, language: str, sheet: dict, research_obj: dict,
           cps: float) -> dict:
    beats = sheet["beats"]
    sid = research_obj["sources"][0]["id"]
    segs: list[dict] = []
    speakers: list[dict] = []
    n = 0

    def add(block: str, role: str, text: str, visual: dict, speaker: str | None = None,
            sfx: list | None = None, src: list | None = None) -> None:
        nonlocal n
        n += 1
        segs.append({"id": f"s{n:03d}", "block_id": block, "role": role, "speaker": speaker, "text": text,
                     "prosody": None, "fx_chain": None, "visual": visual, "sfx": sfx or [],
                     "source_ids": src or []})

    topic_short = topic if len(topic) < 60 else topic[:57] + "…"
    secondary_blocks: set[str] = set()
    if content_type == "myth_vs_fact":
        secondary_blocks = {b["block_id"] for b in beats if b["act"] == "myth"}
    elif content_type in ("investigation", "comparison", "listicle"):
        mids = [b for b in beats[2:-2] if b["act"] in ("context", "rising", "list_item")]
        secondary_blocks = {b["block_id"] for b in mids[1::3]}
    cast = content_type in ("story", "case_study")
    quotes = content_type in ("timeline", "investigation", "explainer")
    if cast:
        speakers += [{"name": "Анна", "kind": "character", "gender": "female", "origin": "native", "era": None},
                     {"name": "Инженер Моррис", "kind": "character", "gender": "male", "origin": "foreign",
                      "era": None}]
    if quotes:
        speakers.append({"name": "The Times, 1912", "kind": "quote_author", "gender": "male", "origin": "foreign",
                         "era": "1912"})
    used_quote = used_dialog = False
    for bi, b in enumerate(beats):
        reg = b["visual_register"]
        visual = _visual_for(reg, topic, bi)
        chars = _target_chars(b["target_duration_s"], cps)
        opener = OPENERS.get(b["act"], OPENERS["rising"]).format(topic=topic, topic_short=topic_short,
                                                                 number="2224")
        role = "secondary_narrator" if b["block_id"] in secondary_blocks else "main_narrator"
        src = [sid] if any(ch.isdigit() for ch in opener) or reg in ("counter", "motion_graphics", "diagram") \
            else []
        sfx = ["impact"] if b["music_cue"] == "impact" else []
        if cast and not used_dialog and b["act"] in ("rising", "twist") and chars > 120:
            add(b["block_id"], role, _fill(opener, chars // 2, bi), visual, sfx=sfx, src=src)
            add(b["block_id"], "character", "Мы не успеем. Я проверила дважды — цифры не сходятся.",
                _visual_for("image", topic, bi + 1), speaker="Анна")
            add(b["block_id"], "character", "Успеем. Если сделать всё по инструкции, мы успеем.",
                _visual_for("stock", topic, bi + 2), speaker="Инженер Моррис")
            used_dialog = True
            continue
        if quotes and not used_quote and b["act"] in ("rising", "reveal", "explanation") and chars > 120:
            add(b["block_id"], role, _fill(opener, chars - 90, bi), visual, sfx=sfx, src=src)
            add(b["block_id"], "quote", "«Катастрофа, которой можно было избежать», — писала газета на следующий "
                "день.", {"marker": "TEXT", "content": "«Катастрофа, которой можно было избежать»"},
                speaker="The Times, 1912", src=[sid])
            used_quote = True
            continue
        if chars > 260:
            half = chars // 2
            add(b["block_id"], role, _fill(opener, half, bi), visual, sfx=sfx, src=src)
            add(b["block_id"], role, _fill(FILLERS[bi % len(FILLERS)], chars - half, bi + 3),
                _visual_for(_next_reg(reg), topic, bi + 7))
        else:
            add(b["block_id"], role, _fill(opener, chars, bi), visual, sfx=sfx, src=src)

    ids = [b["block_id"] for b in beats]
    third = max(1, len(ids) // 3)
    music = [{"from_block": ids[0], "to_block": ids[third - 1], "mood": "tense", "duck_db": -14},
             {"from_block": ids[third], "to_block": ids[2 * third - 1], "mood": "build", "duck_db": -14},
             {"from_block": ids[2 * third], "to_block": ids[-1], "mood": "epic", "duck_db": -12}]
    chapters = [{"block_id": ids[0], "title": "Начало"}, {"block_id": ids[2], "title": "Контекст"},
                {"block_id": ids[len(ids) // 2], "title": "Поворот"}, {"block_id": ids[-2], "title": "Развязка"}]
    shorts = [{"block_ids": ids[0:3], "hook": "Эту деталь пропустили все",
               "outro_hook": "Чем всё закончилось — в полной версии"}]
    mid = len(ids) // 2
    shorts.append({"block_ids": ids[mid:mid + 2], "hook": "Цифра, которая всё меняет",
                   "outro_hook": "Полный разбор — на канале"})
    return {"job_id": job_id, "content_type": content_type, "language": language, "topic": topic,
            "target_duration_s": sheet["target_duration_s"],
            "title_candidates": [{"text": f"Почему {topic_short[:50]}?", "style": "question"},
                                 {"text": f"{topic_short[:55]}: 3 детали", "style": "number"},
                                 {"text": f"Правда о том, как {topic_short[:45]}", "style": "statement"}],
            "speakers": speakers, "beats": beats, "segments": segs,
            "sources": research_obj["sources"], "music_plan": music, "chapters": chapters,
            "shorts_candidates": shorts}


def _next_reg(reg: str) -> str:
    return {"stock": "image", "image": "stock", "archive": "image", "parallax": "image", "text": "stock",
            "diagram": "stock", "counter": "image", "map": "image", "motion_graphics": "image"}.get(reg, "image")


def _visual_for(reg: str, topic: str, i: int) -> dict:
    if reg in ("stock", "map"):
        return {"marker": "STOCK", "query": STOCK_Q.get(reg, "city")}
    if reg in ("image", "archive"):
        return {"marker": "IMAGE", "prompt": IMG_P.format(topic_en=f"topic #{i}")}
    if reg == "parallax":
        return {"marker": "PARALLAX", "prompt": f"wide landscape with clear foreground, scene {i}"}
    if reg in ("counter", "motion_graphics"):
        return {"marker": "COUNTER", "value": [2224, 1912, 46, 1500][i % 4], "suffix": "",
                "label": "ключевое число"}
    if reg == "diagram":
        return {"marker": "CHART", "title": "Сравнение", "data": [{"label": "До", "value": 30},
                                                                   {"label": "После", "value": 70}]}
    return {"marker": "TEXT", "content": "Главный вопрос"}


def metadata(script_obj: Script, platforms: list[str]) -> dict[str, Any]:
    title = script_obj.title_candidates[0].text
    plat = {p: {"title": title[:95], "description": f"{script_obj.topic}. Офлайн-описание.",
                "tags": ["история", "факты"], "hashtags": ["#история", "#факты"]} for p in platforms}
    return {"chosen_title": title, "title_style": script_obj.title_candidates[0].style,
            "thumbnail_text": "Что пошло не так", "platforms": plat}
