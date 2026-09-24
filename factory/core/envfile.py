"""Секреты площадок в .env: чтение, запись с сохранением комментариев и порядка, маскирование.

Значения никогда не печатаются целиком — только маска. Файл получает права 600.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def env_path(root: Path) -> Path:
    return root / ".env"


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = LINE_RE.match(line)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out


def _quote(v: str) -> str:
    return f'"{v}"' if re.search(r"[\s#'\"]", v) else v


def set_env(path: Path, updates: dict[str, str], *, apply_to_process: bool = True) -> list[str]:
    """Обновить/добавить ключи. Существующие строки заменяются на месте, новые — в конец.
    Пустое значение в updates пропускается (не стирает сохранённый секрет). Возвращает изменённые ключи."""
    updates = {k: v.strip() for k, v in updates.items() if v is not None and v.strip() != ""}
    if not updates:
        return []
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    changed: list[str] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = LINE_RE.match(line)
        if m and m.group(1) in updates:
            key = m.group(1)
            if _unquote(m.group(2)) != updates[key]:
                changed.append(key)
            lines[i] = f"{key}={_quote(updates[key])}"
            seen.add(key)
    for key, val in updates.items():
        if key not in seen:
            lines.append(f"{key}={_quote(val)}")
            changed.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    if apply_to_process:
        os.environ.update(updates)
    return changed


def mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}…{value[-2:]} ({len(value)} симв.)"


def get(key: str, env: dict[str, str] | None = None) -> str:
    """Значение из окружения процесса, иначе из переданного словаря .env."""
    return os.environ.get(key) or (env or {}).get(key, "")
