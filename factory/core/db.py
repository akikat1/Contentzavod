"""SQLite: состояние джобов, квоты ключей, история форматов/голосов, публикации, статистика.

Одна база на завод. Все записи идут через короткие транзакции, чтобы демон,
CLI и параллельные ffmpeg-воркеры не блокировали друг друга надолго.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    topic         TEXT NOT NULL,
    content_type  TEXT,
    status        TEXT NOT NULL DEFAULT 'new',   -- new|running|done|quarantined|failed
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    qa_cycles     INTEGER NOT NULL DEFAULT 0,
    meta          TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS stage_runs (
    job_id     TEXT NOT NULL,
    stage      TEXT NOT NULL,
    status     TEXT NOT NULL,                     -- running|done|failed
    attempt    INTEGER NOT NULL DEFAULT 1,
    started_at REAL,
    finished_at REAL,
    error      TEXT,
    summary    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (job_id, stage)
);

CREATE TABLE IF NOT EXISTS keys (
    key_id               TEXT PRIMARY KEY,         -- provider:account:fingerprint
    provider             TEXT NOT NULL,
    account              TEXT NOT NULL,
    fingerprint          TEXT NOT NULL,
    dead                 INTEGER NOT NULL DEFAULT 0,
    dead_reason          TEXT,
    cooldown_until       REAL NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    health               REAL NOT NULL DEFAULT 1.0,
    minute_start         REAL NOT NULL DEFAULT 0,
    minute_requests      INTEGER NOT NULL DEFAULT 0,
    day_key              TEXT NOT NULL DEFAULT '',
    day_requests         INTEGER NOT NULL DEFAULT 0,
    day_tokens           INTEGER NOT NULL DEFAULT 0,
    day_images           INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    last_used_at         REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS format_history (
    job_id       TEXT NOT NULL,
    content_type TEXT NOT NULL,
    act_sequence TEXT NOT NULL,
    registers    TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS voice_history (
    job_id     TEXT NOT NULL,
    role       TEXT NOT NULL,
    speaker    TEXT,
    voice_id   TEXT NOT NULL,
    series     TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scripts_index (
    job_id     TEXT PRIMARY KEY,
    topic      TEXT NOT NULL,
    shingles   TEXT NOT NULL,                      -- json list n-грамм для дедупликации
    embedding  TEXT,                               -- json list float, если был доступен
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    job_id       TEXT NOT NULL,
    platform     TEXT NOT NULL,
    variant      TEXT NOT NULL,                    -- master | short_01 | ...
    status       TEXT NOT NULL DEFAULT 'queued',   -- queued|done|deferred|failed|skipped
    scheduled_at REAL NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    url          TEXT,
    platform_id  TEXT,
    last_error   TEXT,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (job_id, platform, variant)
);

CREATE TABLE IF NOT EXISTS platform_daily (
    platform TEXT NOT NULL,
    day_key  TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, day_key)
);

CREATE TABLE IF NOT EXISTS hook_bank (
    job_id          TEXT PRIMARY KEY,
    content_type    TEXT,
    hook_text       TEXT NOT NULL,
    title           TEXT,
    title_style     TEXT,
    cold_open_s     REAL,
    retention_10s   REAL,
    retention_30s   REAL,
    avg_view_ratio  REAL,
    views           INTEGER,
    ctr             REAL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS beat_stats (
    job_id      TEXT NOT NULL,
    block_id    TEXT NOT NULL,
    act         TEXT NOT NULL,
    duration_s  REAL NOT NULL,
    drop_ratio  REAL,                               -- доля зрителей, ушедших на этом блоке
    PRIMARY KEY (job_id, block_id)
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE — берём блокировку записи сразу, чтобы два процесса
        не выдали один и тот же ключ из пула одновременно."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    # ---- kv ----
    def kv_get(self, k: str, default: Any = None) -> Any:
        row = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(row["v"]) if row else default

    def kv_set(self, k: str, v: Any) -> None:
        self.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (k, json.dumps(v, ensure_ascii=False)))


def now() -> float:
    return time.time()
