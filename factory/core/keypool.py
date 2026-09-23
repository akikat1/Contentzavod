"""Пул API-ключей — главный вычислительный ресурс завода.

Не try/except-цепочка, а учёт квот: для каждого ключа хранится расход в
минутном и суточном окне, cooldown после 429, «здоровье» и признак смерти.
acquire(task_class) выдаёт живой ключ с наибольшим остатком квоты, соблюдая
порядок предпочтения провайдеров для класса задачи.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .config import Config
from .db import DB, now

log = logging.getLogger("factory.keypool")

TASK_CLASSES = ("research", "beatsheet", "script", "metadata", "judge", "image", "embed")


@dataclass(frozen=True)
class ApiKey:
    provider: str
    account: str
    secret: str = field(repr=False)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.secret.encode()).hexdigest()[:12]

    @property
    def key_id(self) -> str:
        return f"{self.provider}:{self.account}:{self.fingerprint}"


class NoKeyAvailable(RuntimeError):
    def __init__(self, task_class: str, retry_at: float | None):
        self.task_class = task_class
        self.retry_at = retry_at
        when = f", ближайший освободится через {retry_at - now():.0f} с" if retry_at else ""
        super().__init__(f"Нет доступного ключа для задачи {task_class}{when}")


def load_keys(cfg: Config) -> list[ApiKey]:
    """Ключи из config/keys.yaml (gitignored) + переменных FACTORY_KEYS_<PROVIDER>=k1,k2."""
    keys: list[ApiKey] = []
    path = cfg.path("llm.keys_file", "config/keys.yaml")
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for i, item in enumerate(raw.get("keys", [])):
            secret = item.get("key") or os.environ.get(item.get("key_env", ""), "")
            if not secret:
                log.warning("Ключ #%d (%s) пуст — пропущен", i, item.get("provider"))
                continue
            keys.append(ApiKey(item["provider"], str(item.get("account", f"acc{i:02d}")), secret.strip()))
    for prov in cfg.section("llm.providers"):
        env = os.environ.get(f"FACTORY_KEYS_{prov.upper()}", "")
        for j, secret in enumerate(s for s in env.split(",") if s.strip()):
            keys.append(ApiKey(prov, f"env{j:02d}", secret.strip()))
    uniq: dict[str, ApiKey] = {k.key_id: k for k in keys}
    return list(uniq.values())


class Lease:
    """Выданный ключ. Стадия обязана сообщить исход через один из методов."""

    def __init__(self, pool: KeyPool, key: ApiKey, model: str, task_class: str):
        self.pool, self.key, self.model, self.task_class = pool, key, model, task_class
        self.closed = False

    @property
    def provider(self) -> str:
        return self.key.provider

    def ok(self, tokens: int = 0) -> None:
        self.pool._report(self.key, "ok", tokens=tokens)
        self.closed = True

    def rate_limited(self, retry_after: float | None = None, daily: bool = False, message: str = "") -> None:
        self.pool._report(self.key, "rate_limited", retry_after=retry_after, daily=daily, message=message)
        self.closed = True

    def auth_failed(self, message: str = "") -> None:
        self.pool._report(self.key, "auth", message=message)
        self.closed = True

    def failed(self, message: str = "") -> None:
        self.pool._report(self.key, "error", message=message)
        self.closed = True


class KeyPool:
    def __init__(self, db: DB, cfg: Config, keys: list[ApiKey]):
        self.db, self.cfg = db, cfg
        self.keys = {k.key_id: k for k in keys}
        self.sync()

    @classmethod
    def from_config(cls, db: DB, cfg: Config) -> KeyPool:
        return cls(db, cfg, load_keys(cfg))

    # ---------- настройки провайдеров ----------
    def provider_cfg(self, provider: str) -> dict:
        return self.cfg.section(f"llm.providers.{provider}")

    def limits(self, provider: str) -> dict:
        return self.provider_cfg(provider).get("limits", {})

    def preference(self, task_class: str) -> list[str]:
        return list(self.cfg.get(f"llm.task_preference.{task_class}", []) or [])

    def model_for(self, provider: str, task_class: str) -> str | None:
        return (self.provider_cfg(provider).get("models") or {}).get(task_class)

    def _day_key(self, provider: str, ts: float | None = None) -> str:
        tz = ZoneInfo(self.provider_cfg(provider).get("day_reset_tz", "UTC"))
        return datetime.fromtimestamp(ts or now(), tz).strftime("%Y-%m-%d")

    def _next_day_reset(self, provider: str) -> float:
        tz = ZoneInfo(self.provider_cfg(provider).get("day_reset_tz", "UTC"))
        cur = datetime.fromtimestamp(now(), tz)
        nxt = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return nxt.timestamp()

    # ---------- синхронизация с БД ----------
    def sync(self) -> None:
        with self.db.tx() as c:
            for k in self.keys.values():
                c.execute(
                    "INSERT OR IGNORE INTO keys(key_id, provider, account, fingerprint) VALUES(?,?,?,?)",
                    (k.key_id, k.provider, k.account, k.fingerprint),
                )

    def _refresh_windows(self, c, row, provider: str) -> dict:
        r = dict(row)
        t = now()
        if t - r["minute_start"] >= 60:
            r["minute_start"], r["minute_requests"] = t, 0
        dk = self._day_key(provider)
        if r["day_key"] != dk:
            r.update(day_key=dk, day_requests=0, day_tokens=0, day_images=0)
        c.execute(
            "UPDATE keys SET minute_start=?, minute_requests=?, day_key=?, day_requests=?, day_tokens=?, day_images=? "
            "WHERE key_id=?",
            (r["minute_start"], r["minute_requests"], r["day_key"], r["day_requests"], r["day_tokens"],
             r["day_images"], r["key_id"]),
        )
        return r

    def _eligible(self, r: dict, lim: dict, est_tokens: int, images: int) -> bool:
        if r["dead"] or r["cooldown_until"] > now():
            return False
        if r["minute_requests"] >= lim.get("rpm", 10**9):
            return False
        if r["day_requests"] >= lim.get("rpd", 10**9):
            return False
        if r["day_tokens"] + est_tokens > lim.get("tpd", 10**12):
            return False
        return not (images and r["day_images"] + images > lim.get("ipd", 10**9))

    @staticmethod
    def _headroom(r: dict, lim: dict) -> float:
        parts = [1 - r["day_requests"] / max(1, lim.get("rpd", 10**9))]
        if "tpd" in lim:
            parts.append(1 - r["day_tokens"] / max(1, lim["tpd"]))
        if "ipd" in lim:
            parts.append(1 - r["day_images"] / max(1, lim["ipd"]))
        return max(0.0, min(parts))

    # ---------- выдача ----------
    def acquire(self, task_class: str, est_tokens: int = 0, images: int = 0,
                exclude: set[str] | None = None) -> Lease:
        exclude = exclude or set()
        with self.db.tx() as c:
            for provider in self.preference(task_class):
                model = self.model_for(provider, task_class)
                if not model:
                    continue
                lim = self.limits(provider)
                rows = c.execute("SELECT * FROM keys WHERE provider=? AND dead=0", (provider,)).fetchall()
                cands = []
                for row in rows:
                    if row["key_id"] not in self.keys or row["key_id"] in exclude:
                        continue
                    r = self._refresh_windows(c, row, provider)
                    if self._eligible(r, lim, est_tokens, images):
                        cands.append(r)
                if not cands:
                    continue
                best = max(cands, key=lambda r: (self._headroom(r, lim) * r["health"], -r["last_used_at"]))
                c.execute(
                    "UPDATE keys SET minute_requests=minute_requests+1, day_requests=day_requests+1, "
                    "day_images=day_images+?, last_used_at=? WHERE key_id=?",
                    (images, now(), best["key_id"]),
                )
                return Lease(self, self.keys[best["key_id"]], model, task_class)
        raise NoKeyAvailable(task_class, self.next_available_at(task_class))

    def next_available_at(self, task_class: str) -> float | None:
        provs = [p for p in self.preference(task_class) if self.model_for(p, task_class)]
        if not provs:
            return None
        q = ",".join("?" * len(provs))
        row = self.db.one(
            f"SELECT MIN(MAX(cooldown_until, minute_start + 60)) AS t FROM keys "
            f"WHERE dead=0 AND provider IN ({q})", tuple(provs))
        return row["t"] if row and row["t"] else None

    # ---------- отчёты ----------
    def _report(self, key: ApiKey, outcome: str, tokens: int = 0, retry_after: float | None = None,
                daily: bool = False, message: str = "") -> None:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM keys WHERE key_id=?", (key.key_id,)).fetchone()
            if row is None:
                return
            cf, health = row["consecutive_failures"], row["health"]
            if outcome == "ok":
                c.execute("UPDATE keys SET day_tokens=day_tokens+?, consecutive_failures=0, health=?, "
                          "last_error=NULL WHERE key_id=?", (tokens, min(1.0, health * 0.9 + 0.1), key.key_id))
            elif outcome == "rate_limited":
                if daily:
                    until = self._next_day_reset(key.provider)
                    self._learn_daily_limit(key.provider, row["day_requests"])
                else:
                    until = now() + max(retry_after or 0, min(3600, 15 * 2 ** cf))
                c.execute("UPDATE keys SET cooldown_until=?, consecutive_failures=?, health=?, last_error=? "
                          "WHERE key_id=?", (until, cf + 1, health * 0.8, f"429 {message}"[:300], key.key_id))
                log.info("Ключ %s → cooldown до %s (%s)", key.key_id,
                         datetime.fromtimestamp(until).strftime("%H:%M:%S"), "сутки" if daily else "минуты")
            elif outcome == "auth":
                c.execute("UPDATE keys SET dead=1, dead_reason=?, last_error=? WHERE key_id=?",
                          (message[:300], message[:300], key.key_id))
                log.error("Ключ %s помечен мёртвым: %s", key.key_id, message)
            else:
                until = now() + min(600, 5 * 2 ** cf)
                c.execute("UPDATE keys SET cooldown_until=?, consecutive_failures=?, health=?, last_error=? "
                          "WHERE key_id=?", (until, cf + 1, health * 0.9, message[:300], key.key_id))

    def _learn_daily_limit(self, provider: str, observed: int) -> None:
        """Free-тиры меняются без предупреждения: запоминаем фактический потолок."""
        k = f"learned_rpd:{provider}"
        prev = self.db.kv_get(k)
        if observed and (prev is None or observed < prev):
            self.db.kv_set(k, observed)

    def status(self) -> list[dict]:
        out = []
        for row in self.db.all("SELECT * FROM keys ORDER BY provider, account"):
            if row["key_id"] not in self.keys:
                continue
            lim = self.limits(row["provider"])
            day_fresh = row["day_key"] == self._day_key(row["provider"])
            out.append({
                "key_id": row["key_id"],
                "provider": row["provider"],
                "account": row["account"],
                "state": "dead" if row["dead"] else ("cooldown" if row["cooldown_until"] > now() else "ok"),
                "day_requests": row["day_requests"] if day_fresh else 0,
                "rpd": lim.get("rpd"),
                "day_tokens": row["day_tokens"] if day_fresh else 0,
                "tpd": lim.get("tpd"),
                "day_images": row["day_images"] if day_fresh else 0,
                "ipd": lim.get("ipd"),
                "health": round(row["health"], 2),
                "last_error": row["last_error"],
                "learned_rpd": self.db.kv_get(f"learned_rpd:{row['provider']}"),
            })
        return out


def append_keys_file(path: Path, provider: str, secrets: list[str], account_prefix: str = "acc") -> int:
    data = {"keys": []}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {"keys": []}
    existing = {k.get("key") for k in data.get("keys", [])}
    n = 0
    base = sum(1 for k in data["keys"] if k.get("provider") == provider)
    for s in secrets:
        s = s.strip()
        if not s or s.startswith("#") or s in existing:
            continue
        n += 1
        data["keys"].append({"provider": provider, "account": f"{account_prefix}{base + n:02d}", "key": s})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.chmod(path, 0o600)
    return n
