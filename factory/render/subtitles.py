"""Субтитры: SRT для загрузки на площадки и ASS с karaoke-подсветкой активного слова.

Слова из [emph] всегда выделены акцентным цветом и кеглем — та же разметка
управляет акцентом в голосе и зум-панчем в монтаже.
"""
from __future__ import annotations

from pathlib import Path


def _ts_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ts_ass(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def group_lines(words: list[dict], max_words: int = 5, max_span: float = 3.2) -> list[list[dict]]:
    """Строки субтитров: не больше N слов, разрыв на конце предложения и на долгой паузе."""
    lines: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur and (len(cur) >= max_words or w["start"] - cur[0]["start"] > max_span
                    or w["start"] - cur[-1]["end"] > 0.6):
            lines.append(cur)
            cur = []
        cur.append(w)
        if w["w"].rstrip("»\"").endswith((".", "!", "?", "…")):
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    return lines


def all_words(timeline: dict, t0: float = 0.0, t1: float | None = None) -> list[dict]:
    out = []
    for seg in timeline["segments"]:
        for w in seg["words"]:
            if w["start"] >= t0 and (t1 is None or w["end"] <= t1 + 0.05):
                out.append({**w, "start": w["start"] - t0, "end": w["end"] - t0})
    return out


def write_srt(words: list[dict], path: Path, max_words: int = 7) -> Path:
    lines = group_lines(words, max_words=max_words, max_span=4.5)
    out = []
    for i, ln in enumerate(lines, 1):
        text = " ".join(w["w"] for w in ln)
        end = ln[-1]["end"] + 0.15
        if i < len(lines):
            end = min(end, lines[i][0]["start"] - 0.01)
        out.append(f"{i}\n{_ts_srt(ln[0]['start'])} --> {_ts_srt(max(end, ln[0]['start'] + 0.3))}\n{text}\n")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def write_ass(words: list[dict], path: Path, width: int, height: int, *, font: str = "DejaVu Sans",
              max_words: int = 5, base_color: str = "&H00FFFFFF", emph_color: str = "&H0000D7FF",
              outline: int = 4, margin_v: int | None = None, karaoke: bool = True,
              active_color: str = "&H0050FF50") -> Path:
    size = int(height * (0.052 if height > width else 0.058))
    mv = margin_v if margin_v is not None else int(height * 0.08)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{base_color},{base_color},&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{outline},1,2,{int(width * 0.06)},{int(width * 0.06)},{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    from .fonts import font as _font  # noqa: PLC0415

    f = _font(size, font)
    limit = width * 0.86
    lines: list[list[dict]] = []
    for ln in group_lines(words, max_words=max_words):
        cur: list[dict] = []          # строка шире кадра (длинные слова) — делим дальше
        for w in ln:
            trial = " ".join(x["w"] for x in cur + [w])
            if cur and f.getlength(trial) * 1.12 > limit:
                lines.append(cur)
                cur = []
            cur.append(w)
        if cur:
            lines.append(cur)
    events = []
    for ln in lines:
        line_end = ln[-1]["end"] + 0.12
        full = f.getlength(" ".join(w["w"] for w in ln)) * 1.12
        fs_tag = r"{\fs" + str(max(12, int(size * limit / full))) + "}" if full > limit else ""   # одно длинное слово
        steps = range(len(ln)) if karaoke else [None]
        for k in steps:
            a = ln[k]["start"] if k is not None else ln[0]["start"]
            b = (ln[k + 1]["start"] if k is not None and k + 1 < len(ln) else line_end)
            if b - a < 0.02:
                continue
            parts = []
            for j, w in enumerate(ln):
                txt = w["w"].replace("{", "(").replace("}", ")")
                if k is not None and j == k:
                    parts.append(r"{\c" + active_color + r"\fscx114\fscy114}" + txt + r"{\r}")
                elif w.get("emph"):
                    parts.append(r"{\c" + emph_color + "}" + txt + r"{\r}")
                else:
                    parts.append(txt)
            events.append(f"Dialogue: 0,{_ts_ass(a)},{_ts_ass(b)},Default,,0,0,0,,{fs_tag}{' '.join(parts)}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path
