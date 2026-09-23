"""Загрузка конфигурации.

Слои (каждый следующий перекрывает предыдущий):
  1. config/factory.yaml        — всё, что коммитится в git
  2. config/local.yaml          — локальные переопределения этой машины (gitignored)
  3. переменные окружения FACTORY__SECTION__KEY=value
  4. overrides из CLI (--set section.key=value)
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _coerce(value: str) -> Any:
    """Строку из env/CLI превращаем в bool/int/float/list по смыслу."""
    low = value.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    if low in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if "," in value:
        return [_coerce(v.strip()) for v in value.split(",")]
    return value


def _set_dotted(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


class Config:
    """Обёртка над dict с доступом по точечному пути: cfg.get("voice.min_role_duration_s")."""

    def __init__(self, data: dict, root: Path = ROOT):
        self.data = data
        self.root = root

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for p in dotted.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur

    def require(self, dotted: str) -> Any:
        val = self.get(dotted)
        if val is None:
            raise KeyError(f"В конфиге нет обязательного параметра {dotted}")
        return val

    def section(self, dotted: str) -> dict:
        val = self.get(dotted, {})
        return val if isinstance(val, dict) else {}

    def set(self, dotted: str, value: Any) -> None:
        _set_dotted(self.data, dotted, value)

    def path(self, dotted: str, default: str | None = None) -> Path:
        raw = self.get(dotted, default)
        if raw is None:
            raise KeyError(dotted)
        p = Path(raw)
        return p if p.is_absolute() else self.root / p

    # ---- удобные ярлыки ----
    @property
    def offline(self) -> bool:
        return bool(self.get("runtime.offline", False))

    @property
    def dry_run(self) -> bool:
        return bool(self.get("runtime.dry_run", False))

    @property
    def language(self) -> str:
        return self.get("channel.language", "ru")


def load_dotenv(path: Path) -> None:
    """Секреты площадок из .env (gitignored). Уже заданные переменные окружения не перетираются."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(overrides: dict[str, Any] | None = None, config_dir: Path | None = None) -> Config:
    cdir = config_dir or CONFIG_DIR
    load_dotenv(cdir.parent / ".env")
    with open(cdir / "factory.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    local = cdir / "local.yaml"
    if local.exists():
        with open(local, encoding="utf-8") as fh:
            data = _deep_merge(data, yaml.safe_load(fh) or {})
    for key, val in os.environ.items():
        if key.startswith("FACTORY__"):
            dotted = key[len("FACTORY__"):].lower().replace("__", ".")
            _set_dotted(data, dotted, _coerce(val))
    for dotted, val in (overrides or {}).items():
        _set_dotted(data, dotted, _coerce(val) if isinstance(val, str) else val)
    return Config(data, root=cdir.parent)


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)
