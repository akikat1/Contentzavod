"""Провайдеры изображений: Nano Banana (через пул ключей) → Pollinations → ComfyUI (SDXL-Turbo
локально) → процедурный фон. Кэш по (промт, вариант, размер) — повторный прогон бесплатен."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import random
import time
import urllib.parse
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageFilter

from ...core.config import Config
from ...core.keypool import KeyPool, NoKeyAvailable
from ..llm.base import AuthError, BadRequest, LLMError, RateLimited
from ..llm.gemini import GeminiClient

log = logging.getLogger("factory.image")


class ImageUnavailable(RuntimeError):
    pass


def _save(data: bytes, path: Path) -> Path:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=93)
    return path


class GeminiImage:
    name = "gemini"

    def __init__(self, cfg: Config, pool: KeyPool | None):
        self.cfg, self.pool = cfg, pool
        self.client = GeminiClient(cfg.get("llm.providers.gemini.base_url"))

    def generate(self, prompt: str, seed: int, w: int, h: int, out: Path) -> Path:
        if self.pool is None or not self.pool.keys:
            raise ImageUnavailable("пул ключей пуст")
        aspect = "16:9" if w > h else "9:16" if h > w else "1:1"
        for _ in range(6):
            try:
                lease = self.pool.acquire("image", images=1)
            except NoKeyAvailable as e:
                raise ImageUnavailable(str(e)) from e
            try:
                data, _mime = self.client.image(prompt=f"{prompt} (variation {seed})", model=lease.model,
                                                api_key=lease.key.secret, aspect_ratio=aspect)
                lease.ok(0)
                return _save(data, out)
            except RateLimited as e:
                lease.rate_limited(e.retry_after, e.daily, str(e))
            except AuthError as e:
                lease.auth_failed(str(e))
            except BadRequest as e:
                lease.ok(0)
                raise ImageUnavailable(f"gemini image: {e}") from e
            except (LLMError, httpx.HTTPError) as e:
                lease.failed(str(e))
        raise ImageUnavailable("gemini image: ключи не дали результата")


class Pollinations:
    """Бесплатный FLUX без ключа. Анонимно ~1 запрос в 15 с — бережём паузой."""
    name = "pollinations"
    _last = 0.0

    def __init__(self, cfg: Config):
        self.base = cfg.get("visual.pollinations.base_url", "https://image.pollinations.ai/prompt")
        self.model = cfg.get("visual.pollinations.model", "flux")

    def generate(self, prompt: str, seed: int, w: int, h: int, out: Path) -> Path:
        wait = 15 - (time.time() - Pollinations._last)
        if wait > 0:
            time.sleep(wait)
        url = f"{self.base}/{urllib.parse.quote(prompt)}"
        params = {"width": w, "height": h, "seed": seed, "nologo": "true", "model": self.model}
        for attempt in range(3):
            Pollinations._last = time.time()
            try:
                r = httpx.get(url, params=params, timeout=180, follow_redirects=True)
            except httpx.HTTPError as e:
                raise ImageUnavailable(f"pollinations: {e}") from e
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
                raise ImageUnavailable(f"pollinations {r.status_code}: {r.text[:120]}")
            return _save(r.content, out)
        raise ImageUnavailable("pollinations: лимит запросов")


class ComfyUI:
    """Локальный SDXL-Turbo через API ComfyUI (запускать с --lowvram на 4 ГБ)."""
    name = "comfyui"

    def __init__(self, cfg: Config):
        self.base = cfg.get("visual.comfyui.base_url", "http://127.0.0.1:8188").rstrip("/")
        self.workflow = cfg.path("visual.comfyui.workflow", "config/comfyui_sdxl_turbo.json")
        self.gpu_lock = cfg.path("paths.gpu_lock", "data/gpu.lock")

    def generate(self, prompt: str, seed: int, w: int, h: int, out: Path) -> Path:
        from ...core.gpu_lock import gpu_session  # noqa: PLC0415
        if not self.workflow.exists():
            raise ImageUnavailable("нет файла workflow ComfyUI")
        wf = json.loads(self.workflow.read_text(encoding="utf-8"))
        text = json.dumps(wf).replace("{{prompt}}", prompt.replace('"', "'"))
        wf = json.loads(text.replace('"{{seed}}"', str(seed)).replace('"{{width}}"', str(min(w, 1024) // 8 * 8))
                        .replace('"{{height}}"', str(min(h, 1024) // 8 * 8)))
        try:
            with gpu_session(self.gpu_lock, "comfyui"):
                r = httpx.post(f"{self.base}/prompt", json={"prompt": wf}, timeout=30)
                r.raise_for_status()
                pid = r.json()["prompt_id"]
                for _ in range(240):
                    hist = httpx.get(f"{self.base}/history/{pid}", timeout=30).json()
                    if pid in hist:
                        for node in hist[pid]["outputs"].values():
                            for im in node.get("images", []):
                                img = httpx.get(f"{self.base}/view", params=im, timeout=60)
                                return _save(img.content, out)
                        raise ImageUnavailable("comfyui: нет изображений в ответе")
                    time.sleep(1)
        except httpx.HTTPError as e:
            raise ImageUnavailable(f"comfyui: {e}") from e
        raise ImageUnavailable("comfyui: таймаут")


class Procedural:
    """Абстрактный фон без текста — последнее дно, чтобы кадр был всегда."""
    name = "procedural"

    def generate(self, prompt: str, seed: int, w: int, h: int, out: Path) -> Path:
        rng = random.Random(int(hashlib.sha256(f"{prompt}:{seed}".encode()).hexdigest()[:8], 16))
        hue = rng.random()
        import colorsys  # noqa: PLC0415
        c1 = np.array(colorsys.hsv_to_rgb(hue, 0.55, 0.35)) * 255
        c2 = np.array(colorsys.hsv_to_rgb((hue + 0.12) % 1, 0.65, 0.8)) * 255
        yy, xx = np.mgrid[0:h, 0:w]
        ang = rng.random() * np.pi
        g = ((xx / w) * np.cos(ang) + (yy / h) * np.sin(ang))
        g = (g - g.min()) / (np.ptp(g) or 1)
        img = c1[None, None, :] * (1 - g[..., None]) + c2[None, None, :] * g[..., None]
        for _ in range(7):   # мягкие «источники света»
            cx, cy, r = rng.random() * w, rng.random() * h, (0.1 + 0.3 * rng.random()) * max(w, h)
            d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
            img += (np.clip(1 - d / r, 0, 1) ** 2)[..., None] * rng.uniform(20, 70)
        img += np.random.default_rng(seed).normal(0, 6, img.shape)
        pil = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
        out.parent.mkdir(parents=True, exist_ok=True)
        pil.save(out, quality=92)
        return out


class ImageRouter:
    def __init__(self, cfg: Config, pool: KeyPool | None):
        self.cfg = cfg
        self.cache = cfg.path("paths.cache", "assets/cache") / "images"
        order = cfg.get("visual.image_providers", ["gemini", "pollinations", "comfyui", "procedural"])
        if cfg.offline:
            order = [p for p in order if p in ("comfyui", "procedural")]
        makers = {"gemini": lambda: GeminiImage(cfg, pool), "pollinations": lambda: Pollinations(cfg),
                  "comfyui": lambda: ComfyUI(cfg), "procedural": Procedural}
        self.providers = [makers[p]() for p in order if p in makers]
        if not any(p.name == "procedural" for p in self.providers):
            self.providers.append(Procedural())
        self.down: set[str] = set()
        self.used: dict[str, int] = {}

    def get(self, prompt: str, seed: int, w: int, h: int) -> tuple[Path, str]:
        key = hashlib.sha256(f"{prompt}|{seed}|{w}x{h}".encode()).hexdigest()[:24]
        for p in self.providers:
            if p.name in self.down:
                continue
            path = self.cache / f"{key}_{p.name}.jpg"
            if path.exists():
                self.used[p.name] = self.used.get(p.name, 0) + 1
                return path, p.name
            try:
                p.generate(prompt, seed, w, h, path)
                self.used[p.name] = self.used.get(p.name, 0) + 1
                return path, p.name
            except ImageUnavailable as e:
                log.warning("%s: %s", p.name, e)
                if p.name != "gemini":
                    self.down.add(p.name)     # до конца джоба не пытаемся снова
        raise ImageUnavailable("ни один провайдер изображений не сработал")
