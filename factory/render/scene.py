"""Рендер одной сцены (шота) в mp4 без звука.

Все сцены кодируются одинаковыми параметрами — поэтому финальная склейка идёт
concat-демуксером без перекодирования, а память не зависит от длины ролика.
Каждый шот рендерится отдельным процессом ffmpeg (посценный рендер: на 16 ГБ ОЗУ
один filter_complex на десятки входов уходит в OOM).
"""
from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from ..core import media
from .fonts import font, font_path

log = logging.getLogger("factory.scene")


@dataclass
class Frame:
    w: int
    h: int
    fps: int
    codec: list[str]


@dataclass
class ShotSpec:
    id: str
    kind: str                      # stock | image | parallax | text | counter | chart
    frames: int
    asset: str | None = None
    depth: str | None = None
    variant: int = 0
    stock_offset: float = 0.0
    text: str = ""
    emph_words: list[str] = field(default_factory=list)
    value: float | None = None
    prefix: str = ""
    suffix: str = ""
    label: str = ""
    title: str = ""
    data: list[dict] = field(default_factory=list)
    grade: dict = field(default_factory=dict)
    punches: list[float] = field(default_factory=list)
    counters: list[dict] = field(default_factory=list)
    fade_in: float = 0.0
    overlay_text: list[dict] = field(default_factory=list)   # [{text, t0, t1, pos}] — для шортсов
    accent: tuple[int, int, int] = (255, 215, 0)


# ------------------------------------------------------------------ фильтры

def grade_filter(g: dict) -> str:
    if not g:
        return ""
    parts = [f"eq=contrast={g.get('contrast', 1):.3f}:saturation={g.get('saturation', 1):.3f}:"
             f"brightness={g.get('brightness', 0):.3f}:gamma={g.get('gamma', 1):.3f}"]
    warm = float(g.get("warmth", 0))
    if abs(warm) > 0.005:
        parts.append(f"colorbalance=rm={warm:.3f}:bm={-warm:.3f}:rh={warm / 2:.3f}:bh={-warm / 2:.3f}")
    if g.get("sepia"):
        parts.append("colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131")
    if g.get("vignette"):
        parts.append("vignette=PI/5")
    if g.get("grain"):
        parts.append(f"noise=alls={int(g['grain'])}:allf=t")
    return ",".join(parts)


def punch_expr(punches: list[float], amp: float, dur: float, tvar: str = "t") -> str:
    """Сумма «горбов» sin(πx) в моменты зум-панчей — масштаб 1 → 1+amp → 1 за dur секунд."""
    if not punches:
        return "0"
    terms = [f"if(between({tvar},{p:.3f},{p + dur:.3f}),sin(PI*({tvar}-{p:.3f})/{dur:.3f}),0)" for p in punches]
    return f"{amp:.4f}*({'+'.join(terms)})"


def _esc_drawtext(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019").replace("%", "\\%").replace(",", "\\,")


def counters_filter(counters: list[dict], fr: Frame) -> str:
    """Анимированный счётчик поверх любого кадра средствами drawtext: %{eif} считает от 0 до значения."""
    fp = font_path()
    out = []
    size = int(fr.h * 0.12)
    for c in counters:
        t0, d, v = c["t"], c.get("dur", 2.2), float(c["value"])
        start = v - 40 if (1000 <= v <= 2100 and float(v).is_integer()) else 0
        expr = f"{start:.0f}+({v - start:.0f})*min(1\\,(t-{t0:.3f})/0.8)"
        text = f"{_esc_drawtext(c.get('prefix', ''))}%{{eif\\:{expr}\\:d}}{_esc_drawtext(c.get('suffix', ''))}"
        out.append(f"drawtext=fontfile='{fp}':text='{text}':fontsize={size}:fontcolor=white:borderw={max(2, size // 18)}:"
                   f"bordercolor=black@0.8:x=(w-text_w)/2:y=h*0.62:enable='between(t,{t0:.3f},{t0 + d:.3f})'")
    return ",".join(out)


def fit_lines(text: str, max_width: float, size: int, max_lines: int = 3) -> tuple[list[str], int]:
    """Перенос по словам и уменьшение кегля, пока текст не влезет в ширину кадра."""
    while size > 12:
        f = font(size)
        lines: list[str] = []
        for w in text.split():
            if lines and f.getlength(lines[-1] + " " + w) <= max_width:
                lines[-1] += " " + w
            else:
                lines.append(w)
        if len(lines) <= max_lines and all(f.getlength(ln) <= max_width for ln in lines):
            return lines, size
        size = int(size * 0.9)
    return [text], size


def overlay_text_filter(items: list[dict], fr: Frame) -> str:
    fp = font_path()
    out = []
    for it in items:
        lines, size = fit_lines(it["text"], fr.w * 0.86, int(fr.w * it.get("scale", 0.075)))
        y0 = {"top": fr.h * 0.12, "center": fr.h * 0.42, "upper": fr.h * 0.22}.get(it.get("pos", "upper"), fr.h * 0.22)
        for k, ln in enumerate(lines):
            y = int(y0 + k * size * 1.45)
            out.append(f"drawtext=fontfile='{fp}':text='{_esc_drawtext(ln)}':fontsize={size}:fontcolor=white:"
                       f"box=1:boxcolor=black@0.55:boxborderw={size // 4}:x=(w-text_w)/2:y={y}:"
                       f"enable='between(t,{it['t0']:.3f},{it['t1']:.3f})'")
    return ",".join(out)


def _post_filters(spec: ShotSpec, fr: Frame, skip_grade: bool = False) -> list[str]:
    f = []
    if not skip_grade and (g := grade_filter(spec.grade)):
        f.append(g)
    if spec.counters:
        f.append(counters_filter(spec.counters, fr))
    if spec.overlay_text:
        f.append(overlay_text_filter(spec.overlay_text, fr))
    if spec.fade_in > 0:
        f.append(f"fade=t=in:st=0:d={spec.fade_in:.3f}")
    f.append("format=yuv420p")
    return f


# ------------------------------------------------------------------ рендеры

def render(spec: ShotSpec, fr: Frame, out: Path, punch_amp: float = 0.08, punch_dur: float = 0.3) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    if spec.kind == "stock" and spec.asset:
        return _render_stock(spec, fr, out, punch_amp, punch_dur)
    if spec.kind in ("image",) and spec.asset:
        return _render_kenburns(spec, fr, out, punch_amp, punch_dur)
    if spec.kind == "parallax" and spec.asset:
        return _pipe_frames(spec, fr, out, _parallax_frames(spec, fr))
    if spec.kind == "counter":
        return _pipe_frames(spec, fr, out, _counter_frames(spec, fr), skip_grade=True)
    if spec.kind == "chart":
        return _pipe_frames(spec, fr, out, _chart_frames(spec, fr), skip_grade=True)
    return _pipe_frames(spec, fr, out, _text_frames(spec, fr), skip_grade=True)


def _render_stock(spec: ShotSpec, fr: Frame, out: Path, amp: float, dur: float) -> Path:
    dur_s = spec.frames / fr.fps
    zoom = punch_expr(spec.punches, amp, dur)
    vf = [f"scale={fr.w}:{fr.h}:force_original_aspect_ratio=increase", f"crop={fr.w}:{fr.h}", f"fps={fr.fps}",
          "setsar=1"]
    if spec.punches:
        vf += [f"scale=w='trunc({fr.w}*(1+{zoom})/2)*2':h='trunc({fr.h}*(1+{zoom})/2)*2':eval=frame",
               f"crop={fr.w}:{fr.h}"]
    vf += _post_filters(spec, fr)
    media.run(["ffmpeg", "-stream_loop", "-1", "-ss", f"{spec.stock_offset:.2f}", "-i", spec.asset,
               "-t", f"{dur_s + 0.5:.3f}", "-vf", ",".join(vf), "-frames:v", str(spec.frames), "-an", *fr.codec,
               "-r", str(fr.fps), str(out)])
    return out


KB_MOVES = [  # (зум старт, зум конец, x-направление, y-направление)
    (1.00, 1.14, 0.0, 0.0), (1.14, 1.00, 0.0, 0.0), (1.10, 1.10, -1.0, 0.0),
    (1.10, 1.10, 1.0, 0.0), (1.02, 1.16, 0.0, -0.6), (1.12, 1.04, 0.5, 0.3),
]


def _render_kenburns(spec: ShotSpec, fr: Frame, out: Path, amp: float, dur: float) -> Path:
    n = max(1, spec.frames)
    z0, z1, dx, dy = KB_MOVES[spec.variant % len(KB_MOVES)]
    fps = fr.fps
    # zoompan считает в номерах кадров (on), поэтому время панча = on/fps
    punch = punch_expr(spec.punches, amp, dur, tvar=f"(on/{fps})")
    z_expr = f"({z0}+({z1 - z0})*on/{n})*(1+{punch})"
    x_expr = f"(iw-iw/zoom)/2+({dx})*(iw-iw/zoom)/2*(2*on/{n}-1)"
    y_expr = f"(ih-ih/zoom)/2+({dy})*(ih-ih/zoom)/2*(2*on/{n}-1)"
    vf = [f"scale={fr.w * 2}:{fr.h * 2}:force_original_aspect_ratio=increase", f"crop={fr.w * 2}:{fr.h * 2}",
          f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:s={fr.w}x{fr.h}:fps={fps}", "setsar=1"]
    vf += _post_filters(spec, fr)
    media.run(["ffmpeg", "-loop", "1", "-framerate", str(fps), "-i", spec.asset, "-vf", ",".join(vf),
               "-frames:v", str(n), "-an", *fr.codec, "-r", str(fps), str(out)])
    return out


def _pipe_frames(spec: ShotSpec, fr: Frame, out: Path, frames, skip_grade: bool = False) -> Path:
    vf = _post_filters(spec, fr, skip_grade=skip_grade)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{fr.w}x{fr.h}", "-r", str(fr.fps), "-i", "-", "-vf", ",".join(vf), "-frames:v", str(spec.frames),
           "-an", *fr.codec, str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for img in frames:
            proc.stdin.write(img.tobytes())
    except BrokenPipeError:
        pass
    finally:
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.wait()
    if proc.returncode != 0:
        raise media.FFmpegError(f"рендер {spec.id}: {err[-1500:]}")
    return out


def _ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


def _bg(fr: Frame, seed: int, t: float) -> Image.Image:
    rng = np.random.default_rng(seed)
    hue_shift = rng.uniform(-20, 20)
    yy = np.linspace(0, 1, fr.h)[:, None]
    xx = np.linspace(0, 1, fr.w)[None, :]
    cx = 0.5 + 0.25 * math.sin(t * 0.6 + seed)
    glow = np.exp(-(((xx - cx) ** 2) / 0.08 + ((yy - 0.45) ** 2) / 0.12))
    base = np.stack([14 + 20 * yy + 30 * glow, 18 + 14 * yy + 26 * glow + hue_shift * 0.2,
                     32 + 30 * yy + 60 * glow], axis=-1)
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))


def _text_frames(spec: ShotSpec, fr: Frame):
    words = spec.text.split() or [" "]
    emph = {w.strip(".,!?:;«»\"").lower() for w in spec.emph_words}
    size = int(fr.h * (0.09 if fr.w > fr.h else 0.06))
    f = font(size)
    lines: list[list[str]] = [[]]
    maxw = fr.w * 0.82
    for w in words:
        trial = " ".join(lines[-1] + [w])
        if lines[-1] and f.getlength(trial) > maxw:
            lines.append([w])
        else:
            lines[-1].append(w)
    per_word = min(0.28, 0.9 * (spec.frames / fr.fps) / max(1, len(words) + 2))
    line_h = int(size * 1.25)
    top = (fr.h - line_h * len(lines)) // 2
    bg0 = _bg(fr, hash(spec.text) % 1000, 0)
    for i in range(spec.frames):
        t = i / fr.fps
        img = bg0.copy()
        d = ImageDraw.Draw(img)
        k = 0
        for li, ln in enumerate(lines):
            width = f.getlength(" ".join(ln))
            x = (fr.w - width) / 2
            y = top + li * line_h
            for w in ln:
                appear = _ease((t - k * per_word) / 0.25)
                if appear > 0:
                    col = spec.accent if w.strip(".,!?:;«»\"").lower() in emph else (245, 245, 245)
                    alpha = int(255 * appear)
                    dy = int((1 - appear) * size * 0.35)
                    d.text((x, y + dy), w, font=f, fill=(*col, alpha), stroke_width=max(1, size // 30),
                           stroke_fill=(0, 0, 0))
                x += f.getlength(w + " ")
                k += 1
        yield img


def _fmt_num(v: float, lang_sep: str = "\u202f") -> str:
    if float(v).is_integer():
        s = f"{int(v):,}".replace(",", lang_sep)
        return s if not (1000 <= v <= 2100) else str(int(v))
    return f"{v:,.1f}".replace(",", lang_sep)


def _counter_frames(spec: ShotSpec, fr: Frame):
    v = float(spec.value or 0)
    start = v - 40 if (1000 <= v <= 2100 and float(v).is_integer()) else 0.0
    big = font(int(fr.h * (0.24 if fr.w > fr.h else 0.13)))
    small = font(int(fr.h * (0.05 if fr.w > fr.h else 0.032)), bold=False)
    bg0 = _bg(fr, int(v) % 997, 0)
    for i in range(spec.frames):
        t = i / fr.fps
        p = _ease(t / 1.2)
        cur = start + (v - start) * p
        cur = round(cur) if float(v).is_integer() else cur
        txt = f"{spec.prefix}{_fmt_num(cur)}{spec.suffix}"
        img = bg0.copy()
        d = ImageDraw.Draw(img)
        scale = 1 + 0.06 * math.sin(math.pi * min(1, max(0, (t - 1.2) / 0.3)))
        bf = big if scale == 1 else font(int(big.size * scale))
        tw = bf.getlength(txt)
        d.text(((fr.w - tw) / 2, fr.h * 0.33), txt, font=bf, fill=spec.accent, stroke_width=max(2, bf.size // 25),
               stroke_fill=(0, 0, 0))
        if spec.label:
            lw = small.getlength(spec.label)
            a = int(255 * _ease((t - 0.5) / 0.4))
            d.text(((fr.w - lw) / 2, fr.h * 0.66), spec.label, font=small, fill=(230, 230, 230, a))
        yield img


def _chart_frames(spec: ShotSpec, fr: Frame):
    data = spec.data or [{"label": "—", "value": 1}]
    vmax = max(float(d["value"]) for d in data) or 1.0
    title_f = font(int(fr.h * 0.06))
    lab_f = font(int(fr.h * 0.04), bold=False)
    val_f = font(int(fr.h * 0.045))
    n = len(data)
    left, right = fr.w * 0.12, fr.w * 0.88
    base_y, top_y = fr.h * 0.82, fr.h * 0.25
    slot = (right - left) / n
    bar_w = slot * 0.55
    bg0 = _bg(fr, 17, 0)
    for i in range(spec.frames):
        t = i / fr.fps
        img = bg0.copy()
        d = ImageDraw.Draw(img)
        if spec.title:
            tw = title_f.getlength(spec.title)
            d.text(((fr.w - tw) / 2, fr.h * 0.08), spec.title, font=title_f, fill=(245, 245, 245))
        d.line([(left, base_y), (right, base_y)], fill=(200, 200, 200), width=max(2, fr.h // 300))
        for k, item in enumerate(data):
            p = _ease((t - 0.15 * k) / 0.9)
            val = float(item["value"])
            hgt = (base_y - top_y) * (val / vmax) * p
            x0 = left + slot * k + (slot - bar_w) / 2
            col = spec.accent if val == vmax else (120, 170, 255)
            d.rectangle([x0, base_y - hgt, x0 + bar_w, base_y], fill=col)
            vt = _fmt_num(round(val * p) if float(val).is_integer() else val * p)
            d.text((x0 + bar_w / 2 - val_f.getlength(vt) / 2, base_y - hgt - val_f.size * 1.3), vt, font=val_f,
                   fill=(255, 255, 255))
            lt = str(item["label"])[:18]
            d.text((x0 + bar_w / 2 - lab_f.getlength(lt) / 2, base_y + lab_f.size * 0.4), lt, font=lab_f,
                   fill=(220, 220, 220))
        yield img


# ------------------------------------------------------------------ параллакс

def heuristic_depth(img: Image.Image) -> np.ndarray:
    """Глубина без нейросети: низ кадра ближе (плоскость земли) + контрастные области ближе фона."""
    g = np.asarray(img.convert("L").resize((256, 144)), dtype=np.float32) / 255
    h, w = g.shape
    ground = np.linspace(0, 1, h)[:, None] ** 1.3 * np.ones((1, w))
    edges = np.abs(np.asarray(Image.fromarray((g * 255).astype(np.uint8)).filter(ImageFilter.FIND_EDGES),
                              dtype=np.float32) / 255)
    local = np.asarray(Image.fromarray((edges * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(8)),
                       dtype=np.float32) / 255
    d = 0.7 * ground + 0.3 * (local / (local.max() or 1))
    d = np.asarray(Image.fromarray((d / d.max() * 255).astype(np.uint8)).resize(img.size, Image.BILINEAR)
                   .filter(ImageFilter.GaussianBlur(6)), dtype=np.float32) / 255
    return d


def _parallax_frames(spec: ShotSpec, fr: Frame):
    img = Image.open(spec.asset).convert("RGB")
    scale = 1.18
    W, H = int(fr.w * scale), int(fr.h * scale)
    ar = img.width / img.height
    if ar > W / H:
        img = img.resize((int(H * ar), H), Image.LANCZOS)
    else:
        img = img.resize((W, int(W / ar)), Image.LANCZOS)
    img = img.crop(((img.width - W) // 2, (img.height - H) // 2, (img.width - W) // 2 + W, (img.height - H) // 2 + H))
    if spec.depth and Path(spec.depth).exists():
        depth = np.asarray(Image.open(spec.depth).convert("L").resize((W, H)), dtype=np.float32) / 255
    else:
        depth = heuristic_depth(img)
    q1, q2 = np.quantile(depth, [0.45, 0.78])
    layers = []
    rgba = img.convert("RGBA")
    for lo, factor in ((q1, 0.55), (q2, 1.0)):
        mask = Image.fromarray(((depth >= lo) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(4))
        layer = rgba.copy()
        layer.putalpha(mask)
        layers.append((layer, factor))
    base = img
    direction = -1 if spec.variant % 2 else 1
    travel = 0.06 * fr.w
    n = max(1, spec.frames)
    for i in range(n):
        p = i / max(1, n - 1)
        shift = direction * travel * (p - 0.5)
        zoom = 1 + 0.04 * p
        frame = base.crop((int((W - fr.w) / 2 - shift * 0.25), int((H - fr.h) / 2),
                           int((W - fr.w) / 2 - shift * 0.25) + fr.w, int((H - fr.h) / 2) + fr.h)).convert("RGBA")
        for layer, factor in layers:
            lz = layer if zoom == 1 else layer.resize((int(W * (1 + (zoom - 1) * factor)),
                                                       int(H * (1 + (zoom - 1) * factor))), Image.BILINEAR)
            ox = int((lz.width - fr.w) / 2 - shift * factor)
            oy = int((lz.height - fr.h) / 2)
            frame.alpha_composite(lz.crop((ox, oy, ox + fr.w, oy + fr.h)))
        yield frame.convert("RGB")
