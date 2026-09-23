"""JSON-контракт сценария — позвоночник завода.

Все стадии после s04 читают только этот контракт. Модели строгие: невалидный
выход LLM не проходит дальше, а возвращается модели с текстом ошибки.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ContentType = Literal[
    "explainer", "investigation", "listicle", "story",
    "myth_vs_fact", "timeline", "comparison", "case_study",
]
CONTENT_TYPES: tuple[str, ...] = ContentType.__args__  # type: ignore[attr-defined]

Role = Literal["main_narrator", "secondary_narrator", "character", "quote"]
NARRATOR_ROLES = ("main_narrator", "secondary_narrator")
DIRECT_SPEECH_ROLES = ("character", "quote")

Act = Literal[
    "cold_open", "promise", "context", "rising", "list_item", "myth", "debunk",
    "twist", "reveal", "climax", "explanation", "resolution", "finale",
]
ACTS: tuple[str, ...] = Act.__args__  # type: ignore[attr-defined]

VisualRegister = Literal[
    "stock", "archive", "image", "parallax", "motion_graphics", "text", "map", "diagram", "counter",
]
REGISTERS: tuple[str, ...] = VisualRegister.__args__  # type: ignore[attr-defined]

VisualMarker = Literal["STOCK", "IMAGE", "PARALLAX", "CHART", "COUNTER", "TEXT"]
BeatMusicCue = Literal["none", "silence_before", "impact", "riser", "drop"]
Mood = Literal["tense", "calm", "build", "epic", "reflective", "neutral", "dark", "uplifting"]
TitleStyle = Literal["question", "number", "statement", "contrarian", "story", "how"]

BLOCK_ID_RE = re.compile(r"^b\d{2}$")
MARKUP_TOKEN_RE = re.compile(r"\[(/?)(emph|slow|breath|pause)(?::(\d+))?\]")


class Beat(BaseModel):
    block_id: str
    act: Act
    intent: str = Field(min_length=3)
    target_duration_s: float = Field(ge=3, le=90)
    tension: float = Field(ge=0.0, le=1.0)
    visual_register: VisualRegister
    music_cue: BeatMusicCue = "none"
    opens_loop: str | None = None
    closes_loop: str | None = None
    callback_to: str | None = None

    @field_validator("block_id")
    @classmethod
    def _bid(cls, v: str) -> str:
        if not BLOCK_ID_RE.match(v):
            raise ValueError(f"block_id должен иметь вид b01..b99, получено {v!r}")
        return v


class BeatSheet(BaseModel):
    topic: str
    content_type: ContentType
    angle: str
    hook: str
    target_duration_s: float = Field(ge=60, le=900)
    beats: list[Beat] = Field(min_length=5)

    @model_validator(mode="after")
    def _unique(self) -> BeatSheet:
        ids = [b.block_id for b in self.beats]
        if len(ids) != len(set(ids)):
            raise ValueError("block_id в beat sheet повторяются")
        return self

    @property
    def total_target_s(self) -> float:
        return sum(b.target_duration_s for b in self.beats)


class Prosody(BaseModel):
    rate: str | None = None      # "+5%", "-8%"
    pitch: str | None = None     # "+2st", "-1st", "+10Hz"
    volume: str | None = None    # "+10%"

    @field_validator("rate", "volume")
    @classmethod
    def _pct(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[+-]\d{1,2}%", v):
            raise ValueError(f"ожидается формат +5% / -8%, получено {v!r}")
        return v

    @field_validator("pitch")
    @classmethod
    def _pitch(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[+-]\d{1,2}(st|Hz)", v):
            raise ValueError(f"ожидается формат +2st / -10Hz, получено {v!r}")
        return v


class ChartItem(BaseModel):
    label: str
    value: float


class Visual(BaseModel):
    marker: VisualMarker
    query: str | None = None         # STOCK
    prompt: str | None = None        # IMAGE / PARALLAX
    content: str | None = None       # TEXT
    value: float | None = None       # COUNTER
    prefix: str | None = None
    suffix: str | None = None
    label: str | None = None
    title: str | None = None         # CHART
    data: list[ChartItem] | None = None

    @model_validator(mode="after")
    def _fields_for_marker(self) -> Visual:
        need = {
            "STOCK": ("query",), "IMAGE": ("prompt",), "PARALLAX": ("prompt",),
            "TEXT": ("content",), "COUNTER": ("value",), "CHART": ("data",),
        }[self.marker]
        missing = [f for f in need if getattr(self, f) in (None, "", [])]
        if missing:
            raise ValueError(f"маркер {self.marker} требует поля {missing}")
        return self


class Segment(BaseModel):
    id: str
    block_id: str
    role: Role
    speaker: str | None = None
    text: str = Field(min_length=1)
    prosody: Prosody | None = None
    fx_chain: str | None = None
    visual: Visual
    sfx: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _speaker(self) -> Segment:
        if self.role in DIRECT_SPEECH_ROLES and not self.speaker:
            raise ValueError(f"сегмент {self.id}: роль {self.role} требует speaker")
        _check_markup(self.id, self.text)
        return self


class Speaker(BaseModel):
    name: str
    kind: Literal["character", "quote_author"]
    gender: Literal["male", "female", "unknown"] = "unknown"
    origin: Literal["native", "foreign"] = "native"
    era: str | None = None           # "1912" — для архивной обработки цитат


class Source(BaseModel):
    id: str
    url: str
    title: str = ""
    claim: str = ""


class MusicRange(BaseModel):
    from_block: str
    to_block: str
    mood: Mood
    duck_db: float = Field(default=-14, ge=-30, le=0)


class Chapter(BaseModel):
    block_id: str
    title: str


class ShortCandidate(BaseModel):
    block_ids: list[str] = Field(min_length=1)
    hook: str
    outro_hook: str = ""


class TitleCandidate(BaseModel):
    text: str
    style: TitleStyle = "statement"


class VoicePlan(BaseModel):
    strategy: str
    assignments: dict[str, str]                       # role -> voice_id
    speaker_voices: dict[str, str] = Field(default_factory=dict)
    decisions: list[str] = Field(default_factory=list)


class Script(BaseModel):
    job_id: str
    content_type: ContentType
    language: str = "ru"
    topic: str
    title_candidates: list[TitleCandidate] = Field(min_length=1)
    target_duration_s: float
    speakers: list[Speaker] = Field(default_factory=list)
    voice_plan: VoicePlan | None = None
    beats: list[Beat] = Field(min_length=5)
    segments: list[Segment] = Field(min_length=5)
    sources: list[Source] = Field(default_factory=list)
    music_plan: list[MusicRange] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    shorts_candidates: list[ShortCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> Script:
        problems = script_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def beat(self, block_id: str) -> Beat:
        for b in self.beats:
            if b.block_id == block_id:
                return b
        raise KeyError(block_id)

    def block_order(self) -> dict[str, int]:
        return {b.block_id: i for i, b in enumerate(self.beats)}


def _check_markup(seg_id: str, text: str) -> None:
    stack: list[str] = []
    for m in MARKUP_TOKEN_RE.finditer(text):
        closing, tag, num = m.group(1), m.group(2), m.group(3)
        if tag == "pause":
            if closing or num is None or not (50 <= int(num) <= 3000):
                raise ValueError(f"сегмент {seg_id}: [pause:N] требует N от 50 до 3000 мс")
        elif tag == "breath":
            if closing:
                raise ValueError(f"сегмент {seg_id}: у [breath] нет закрывающего тега")
        elif closing:
            if not stack or stack[-1] != tag:
                raise ValueError(f"сегмент {seg_id}: лишний [/{tag}]")
            stack.pop()
        else:
            stack.append(tag)
    if stack:
        raise ValueError(f"сегмент {seg_id}: не закрыт [{stack[-1]}]")


def script_problems(s: Script) -> list[str]:
    """Межполевые проверки. Возвращает человекочитаемые проблемы для ретрая LLM."""
    problems: list[str] = []
    order = {b.block_id: i for i, b in enumerate(s.beats)}
    if len(order) != len(s.beats):
        problems.append("block_id в beats повторяются")
    seg_ids = [seg.id for seg in s.segments]
    if len(seg_ids) != len(set(seg_ids)):
        problems.append("id сегментов повторяются")
    last = -1
    for seg in s.segments:
        if seg.block_id not in order:
            problems.append(f"сегмент {seg.id} ссылается на несуществующий блок {seg.block_id}")
            continue
        if order[seg.block_id] < last:
            problems.append(f"сегмент {seg.id} нарушает порядок блоков")
        last = max(last, order[seg.block_id])
    used = {seg.block_id for seg in s.segments}
    for b in s.beats:
        if b.block_id not in used:
            problems.append(f"у блока {b.block_id} нет ни одного сегмента")
    src_ids = {src.id for src in s.sources}
    for seg in s.segments:
        for sid in seg.source_ids:
            if sid not in src_ids:
                problems.append(f"сегмент {seg.id} ссылается на неизвестный источник {sid}")
    speakers = {sp.name for sp in s.speakers}
    for seg in s.segments:
        if seg.speaker and seg.role in DIRECT_SPEECH_ROLES and seg.speaker not in speakers:
            problems.append(f"сегмент {seg.id}: speaker {seg.speaker!r} не описан в speakers")
    for mr in s.music_plan:
        if mr.from_block not in order or mr.to_block not in order:
            problems.append(f"music_plan ссылается на неизвестный блок {mr.from_block}-{mr.to_block}")
        elif order[mr.from_block] > order[mr.to_block]:
            problems.append(f"music_plan: {mr.from_block} идёт после {mr.to_block}")
    for ch in s.chapters:
        if ch.block_id not in order:
            problems.append(f"глава ссылается на неизвестный блок {ch.block_id}")
    for sc in s.shorts_candidates:
        idx = [order.get(b) for b in sc.block_ids]
        if None in idx:
            problems.append(f"кандидат в шортсы ссылается на неизвестный блок {sc.block_ids}")
        elif idx != list(range(min(idx), max(idx) + 1)):
            problems.append(f"кандидат в шортсы {sc.block_ids} должен состоять из подряд идущих блоков")
    return problems


def plain_text(text: str) -> str:
    """Текст без разметки — для субтитров, подсчёта длительности и дедупликации."""
    return re.sub(r"\s+", " ", MARKUP_TOKEN_RE.sub("", text)).strip()


class Fact(BaseModel):
    id: str
    claim: str
    source_id: str
    numbers: list[str] = Field(default_factory=list)


class Research(BaseModel):
    topic: str
    angle: str
    summary: str
    facts: list[Fact] = Field(min_length=3)
    sources: list[Source] = Field(min_length=1)
    surprising: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refs(self) -> Research:
        ids = {s.id for s in self.sources}
        bad = [f.id for f in self.facts if f.source_id not in ids]
        if bad:
            raise ValueError(f"факты {bad} ссылаются на неизвестные источники")
        return self


class TopicPick(BaseModel):
    topic: str
    angle: str
    content_type: ContentType
    why: str = ""


class PlatformMeta(BaseModel):
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)


class Metadata(BaseModel):
    chosen_title: str
    title_style: TitleStyle = "statement"
    thumbnail_text: str
    platforms: dict[str, PlatformMeta]
