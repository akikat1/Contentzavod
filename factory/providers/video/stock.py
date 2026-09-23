"""Стоковое видео: Pexels (200 запросов/ч, 20K/мес, без атрибуции) и Pixabay (~100/мин).
Один клип не повторяется внутри ролика; выбор по близости цвета — для match cut."""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from ...core.config import Config

log = logging.getLogger("factory.stock")


class StockUnavailable(RuntimeError):
    pass


@dataclass
class Clip:
    provider: str
    clip_id: str
    url: str
    width: int
    height: int
    duration: float
    thumb: str = ""


class Pexels:
    name = "pexels"

    def __init__(self, cfg: Config):
        self.key = os.environ.get(cfg.get("visual.pexels.key_env", "PEXELS_API_KEY"), "")
        self.per_page = int(cfg.get("visual.pexels.per_page", 12))

    def search(self, query: str, orientation: str) -> list[Clip]:
        if not self.key:
            raise StockUnavailable("нет PEXELS_API_KEY")
        r = httpx.get("https://api.pexels.com/videos/search", headers={"Authorization": self.key}, timeout=30,
                      params={"query": query, "per_page": self.per_page, "orientation": orientation})
        if r.status_code == 429:
            raise StockUnavailable("pexels: лимит запросов")
        r.raise_for_status()
        out = []
        for v in r.json().get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("width")]
            files.sort(key=lambda f: abs(f["width"] - 1920))
            if files:
                f = files[0]
                out.append(Clip("pexels", str(v["id"]), f["link"], f["width"], f["height"], float(v.get("duration", 0)),
                                v.get("image", "")))
        return out


class Pixabay:
    name = "pixabay"

    def __init__(self, cfg: Config):
        self.key = os.environ.get(cfg.get("visual.pixabay.key_env", "PIXABAY_API_KEY"), "")
        self.per_page = int(cfg.get("visual.pixabay.per_page", 12))

    def search(self, query: str, orientation: str) -> list[Clip]:
        if not self.key:
            raise StockUnavailable("нет PIXABAY_API_KEY")
        r = httpx.get("https://pixabay.com/api/videos/", timeout=30,
                      params={"key": self.key, "q": query, "per_page": self.per_page, "safesearch": "true"})
        if r.status_code == 429:
            raise StockUnavailable("pixabay: лимит запросов")
        r.raise_for_status()
        out = []
        for h in r.json().get("hits", []):
            v = h.get("videos", {})
            f = v.get("large") if (v.get("large") or {}).get("url") else v.get("medium")
            if f and f.get("url"):
                out.append(Clip("pixabay", str(h["id"]), f["url"], f.get("width", 0), f.get("height", 0),
                                float(h.get("duration", 0)), f.get("thumbnail", "")))
        return out


class StockRouter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache = cfg.path("paths.cache", "assets/cache") / "stock"
        self.cache.mkdir(parents=True, exist_ok=True)
        names = [] if cfg.offline else cfg.get("visual.stock_providers", ["pexels", "pixabay"])
        makers = {"pexels": Pexels, "pixabay": Pixabay}
        self.providers = [makers[n](cfg) for n in names if n in makers]
        self.used_ids: set[str] = set()
        self._search_cache: dict[tuple, list[Clip]] = {}
        self.down: set[str] = set()

    def candidates(self, query: str, orientation: str = "landscape", min_duration: float = 0) -> list[Clip]:
        out = []
        for p in self.providers:
            if p.name in self.down:
                continue
            key = (p.name, query, orientation)
            if key not in self._search_cache:
                try:
                    self._search_cache[key] = p.search(query, orientation)
                except (StockUnavailable, httpx.HTTPError) as e:
                    log.warning("%s: %s", p.name, e)
                    if isinstance(e, StockUnavailable) and "нет " in str(e):
                        self.down.add(p.name)
                    self._search_cache[key] = []
            out += [c for c in self._search_cache[key] if f"{c.provider}:{c.clip_id}" not in self.used_ids
                    and c.duration >= min_duration]
            if out:
                break
        if not out:
            raise StockUnavailable(f"сток по запросу {query!r} не найден")
        return out

    def download(self, clip: Clip) -> Path:
        self.used_ids.add(f"{clip.provider}:{clip.clip_id}")
        path = self.cache / f"{clip.provider}_{clip.clip_id}_{hashlib.md5(clip.url.encode()).hexdigest()[:6]}.mp4"
        if path.exists() and path.stat().st_size > 0:
            return path
        tmp = path.with_suffix(".part")
        with httpx.stream("GET", clip.url, timeout=120, follow_redirects=True) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
        tmp.replace(path)
        return path
