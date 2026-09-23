"""Разбор инлайн-разметки сценария в план синтеза.

[pause:N] и [breath] — точки разреза: между юнитами вставляется тишина/вдох.
[slow]…[/slow] — отдельный юнит с замедлением.
[emph]…[/emph] НЕ режет синтез: разрез посреди фразы ломает интонацию (каждый
кусок получает интонацию конца предложения). Акцент реализуется после синтеза —
+2 дБ на отрезке слова по пословным таймингам — и в субтитрах/монтаже.
Предложения синтезируются отдельно: так работает пофразная вариация просодии.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TOKEN_RE = re.compile(r"\[(/?)(emph|slow|breath|pause)(?::(\d+))?\]")
SENT_RE = re.compile(r"(?<=[.!?…»])\s+(?=[«\"A-ZА-ЯЁ0-9—\[])")


@dataclass
class Word:
    text: str
    emph: bool = False


@dataclass
class Unit:
    """Кусок речи для одного вызова TTS."""
    text: str
    words: list[Word]
    slow: bool = False
    is_question: bool = False
    ends_sentence: bool = True
    pause_before_ms: int = 0
    breath_before: bool = False


@dataclass
class SegmentPlan:
    units: list[Unit] = field(default_factory=list)
    trailing_pause_ms: int = 0


def _words_with_emph(fragment: str, emph_state: bool) -> tuple[list[Word], bool]:
    words: list[Word] = []
    pos = 0
    state = emph_state
    for m in TOKEN_RE.finditer(fragment):
        chunk = fragment[pos:m.start()]
        words += [Word(w, state) for w in chunk.split()]
        if m.group(2) == "emph":
            state = not m.group(1)
        pos = m.end()
    words += [Word(w, state) for w in fragment[pos:].split()]
    merged: list[Word] = []
    for w in words:                      # «минут[/emph].» → точка приклеивается к слову
        if merged and re.fullmatch(r"[.,!?:;…»\")]+", w.text):
            merged[-1].text += w.text
        else:
            merged.append(w)
    return merged, state


def parse(text: str) -> SegmentPlan:
    plan = SegmentPlan()
    pending_pause, pending_breath = 0, False
    slow = False
    emph = False
    buf = ""

    def flush(buf_text: str, is_slow: bool) -> None:
        nonlocal pending_pause, pending_breath, emph
        raw = buf_text.strip()
        if not raw:
            return
        sentences = [s for s in SENT_RE.split(raw) if s.strip()]
        for i, sent in enumerate(sentences):
            words, emph = _words_with_emph(sent, emph)
            clean = " ".join(w.text for w in words)
            if not clean:
                continue
            plan.units.append(Unit(
                text=clean, words=words, slow=is_slow, is_question=clean.rstrip("»\"").endswith("?"),
                ends_sentence=bool(re.search(r"[.!?…]»?$", clean)) or i < len(sentences) - 1,
                pause_before_ms=pending_pause if i == 0 else 0,
                breath_before=pending_breath if i == 0 else False))
            if i == 0:
                pending_pause, pending_breath = 0, False

    pos = 0
    for m in TOKEN_RE.finditer(text):
        closing, tag, num = m.group(1), m.group(2), m.group(3)
        if tag == "emph":
            continue                         # emph остаётся в тексте буфера и разбирается пословно
        buf += text[pos:m.start()]
        pos = m.end()
        if tag == "pause":
            flush(buf, slow)
            buf = ""
            pending_pause += int(num)
        elif tag == "breath":
            flush(buf, slow)
            buf = ""
            pending_breath = True
        elif tag == "slow":
            flush(buf, slow)
            buf = ""
            slow = not closing
    buf += text[pos:]
    flush(buf, slow)
    plan.trailing_pause_ms = pending_pause
    return plan
