"""Банк хуков и статистика блоков: что держало зрителя — подаётся в следующий beat sheet."""
from __future__ import annotations

from ..core.db import DB, now


def record_hook(db: DB, job_id: str, content_type: str, hook: str, title: str, title_style: str,
                cold_open_s: float) -> None:
    db.execute(
        "INSERT INTO hook_bank(job_id, content_type, hook_text, title, title_style, cold_open_s, updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET hook_text=excluded.hook_text, "
        "title=excluded.title, title_style=excluded.title_style, cold_open_s=excluded.cold_open_s, "
        "updated_at=excluded.updated_at",
        (job_id, content_type, hook, title, title_style, cold_open_s, now()))


def update_stats(db: DB, job_id: str, **metrics) -> None:
    allowed = {"retention_10s", "retention_30s", "avg_view_ratio", "views", "ctr"}
    sets = {k: v for k, v in metrics.items() if k in allowed and v is not None}
    if not sets:
        return
    cols = ", ".join(f"{k}=?" for k in sets)
    db.execute(f"UPDATE hook_bank SET {cols}, updated_at=? WHERE job_id=?", (*sets.values(), now(), job_id))


def lessons_for_prompt(db: DB, limit: int = 5) -> str:
    """Короткая сводка для контекста LLM: лучшие и худшие хуки, какие блоки теряли зрителя."""
    best = db.all("SELECT hook_text, retention_30s, cold_open_s, content_type FROM hook_bank "
                  "WHERE retention_30s IS NOT NULL ORDER BY retention_30s DESC LIMIT ?", (limit,))
    worst = db.all("SELECT hook_text, retention_30s FROM hook_bank WHERE retention_30s IS NOT NULL "
                   "ORDER BY retention_30s ASC LIMIT 3")
    drops = db.all("SELECT act, AVG(drop_ratio) AS d, COUNT(*) AS n, AVG(duration_s) AS dur FROM beat_stats "
                   "WHERE drop_ratio IS NOT NULL GROUP BY act HAVING n >= 2 ORDER BY d DESC LIMIT 4")
    styles = db.all("SELECT title_style, AVG(ctr) AS ctr, COUNT(*) AS n FROM hook_bank WHERE ctr IS NOT NULL "
                    "GROUP BY title_style ORDER BY ctr DESC")
    if not (best or drops or styles):
        return ""
    lines = ["Статистика прошлых выпусков канала (используй как ориентир, не копируй):"]
    for r in best:
        lines.append(f"+ держал {r['retention_30s']:.0%} на 30 с ({r['content_type']}, cold open "
                     f"{r['cold_open_s'] or 0:.0f} с): «{r['hook_text'][:140]}»")
    for r in worst:
        lines.append(f"- слабый хук, {r['retention_30s']:.0%} на 30 с: «{r['hook_text'][:140]}»")
    for r in drops:
        lines.append(f"! блоки типа {r['act']} (в среднем {r['dur']:.0f} с) теряют {r['d']:.0%} зрителей — "
                     f"делай их короче и конкретнее")
    for r in styles:
        lines.append(f"* заголовки стиля {r['title_style']}: CTR {r['ctr']:.1%} на {r['n']} выпусках")
    return "\n".join(lines)


def best_title_style(db: DB, candidates: list[str], epsilon: float = 0.2, seed: int = 0) -> str | None:
    """ε-жадный выбор стиля заголовка по истории CTR (A/B без ручного анализа)."""
    import random
    rows = {r["title_style"]: r["ctr"] for r in db.all(
        "SELECT title_style, AVG(ctr) AS ctr FROM hook_bank WHERE ctr IS NOT NULL GROUP BY title_style")}
    known = [c for c in candidates if c in rows]
    rng = random.Random(seed)
    if not known or rng.random() < epsilon:
        untested = [c for c in candidates if c not in rows]
        return rng.choice(untested or candidates) if candidates else None
    return max(known, key=lambda c: rows[c])
