"""Ротация форматов передачи — разнообразие и защита от «шаблонного» контента."""
from __future__ import annotations

import difflib
import hashlib
import json
import random
from pathlib import Path

from ..core.config import Config, load_yaml
from ..core.db import DB, now
from ..core.schema import CONTENT_TYPES


def load_format(cfg: Config, content_type: str) -> dict:
    path = cfg.path("formats.dir", "config/formats") / f"{content_type}.yaml"
    return load_yaml(path)


def recent_formats(db: DB, n: int) -> list[str]:
    rows = db.all("SELECT content_type FROM format_history ORDER BY created_at DESC LIMIT ?", (n,))
    return [r["content_type"] for r in rows]


def choose_format(db: DB, cfg: Config, suggested: str | None = None, seed: str = "") -> tuple[str, str]:
    gap = int(cfg.get("formats.min_gap", 3))
    blocked = set(recent_formats(db, gap))
    if suggested in CONTENT_TYPES and suggested not in blocked:
        return suggested, f"формат {suggested} предложен трендами и не использовался последние {gap} выпуска"
    weights = cfg.section("formats.weights")
    usage20 = recent_formats(db, 20)
    pool = [c for c in CONTENT_TYPES if c not in blocked] or list(CONTENT_TYPES)
    scored = [(c, float(weights.get(c, 1.0)) / (1 + usage20.count(c))) for c in pool]
    rng = random.Random(int(hashlib.sha256((seed or str(now())).encode()).hexdigest()[:8], 16))
    total = sum(w for _, w in scored)
    x, acc = rng.uniform(0, total), 0.0
    for c, w in scored:
        acc += w
        if x <= acc:
            reason = (f"формат {c} выбран ротацией (исключены недавние: {sorted(blocked) or '—'})"
                      + (f"; предложенный {suggested} был недавно" if suggested else ""))
            return c, reason
    return scored[-1][0], "ротация"


def record_format(db: DB, job_id: str, content_type: str, acts: list[str], registers: list[str]) -> None:
    db.execute("DELETE FROM format_history WHERE job_id=?", (job_id,))
    db.execute("INSERT INTO format_history(job_id, content_type, act_sequence, registers, created_at) "
               "VALUES(?,?,?,?,?)", (job_id, content_type, json.dumps(acts), json.dumps(registers), now()))


def template_similarity(db: DB, job_id: str, acts: list[str], registers: list[str], n: int = 5) -> list[dict]:
    """Насколько структура и визуальный ритм совпадают с последними выпусками."""
    rows = db.all("SELECT * FROM format_history WHERE job_id != ? ORDER BY created_at DESC LIMIT ?", (job_id, n))
    out = []
    for r in rows:
        a = difflib.SequenceMatcher(None, acts, json.loads(r["act_sequence"])).ratio()
        g = difflib.SequenceMatcher(None, registers, json.loads(r["registers"])).ratio()
        out.append({"job_id": r["job_id"], "content_type": r["content_type"], "acts": round(a, 2),
                    "registers": round(g, 2)})
    return out


def formats_catalog(cfg: Config) -> dict[str, dict]:
    d = cfg.path("formats.dir", "config/formats")
    return {Path(p).stem: load_yaml(p) for p in sorted(d.glob("*.yaml"))}
