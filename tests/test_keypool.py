import time

import pytest

from factory.core.keypool import ApiKey, KeyPool, NoKeyAvailable


@pytest.fixture
def pool(cfg, db):
    cfg.set("llm.providers.gemini.limits", {"rpm": 2, "rpd": 5, "tpd": 1000, "ipd": 3})
    cfg.set("llm.providers.groq.limits", {"rpm": 30, "rpd": 1000, "tpd": 200000})
    keys = [ApiKey("gemini", "a1", "g-secret-1"), ApiKey("gemini", "a2", "g-secret-2"),
            ApiKey("groq", "a1", "q-secret-1")]
    return KeyPool(db, cfg, keys)


def test_prefers_provider_order_and_spreads_load(pool):
    a = pool.acquire("script")
    a.ok(10)
    b = pool.acquire("script")
    b.ok(10)
    assert a.provider == b.provider == "gemini"
    assert a.key.key_id != b.key.key_id, "нагрузка должна распределяться по ключам с большим остатком"


def test_rpm_exhaustion_moves_to_next_provider(pool):
    for _ in range(4):                      # 2 ключа × rpm 2
        pool.acquire("script").ok(1)
    assert pool.acquire("script").provider == "groq"


def test_rate_limit_puts_key_on_cooldown(pool):
    lease = pool.acquire("script")
    lease.rate_limited(retry_after=120)
    nxt = pool.acquire("script")
    assert nxt.key.key_id != lease.key.key_id


def test_daily_limit_cooldown_until_reset_and_learning(pool, db):
    lease = pool.acquire("script")
    lease.rate_limited(daily=True, message="GenerateRequestsPerDay")
    row = db.one("SELECT cooldown_until FROM keys WHERE key_id=?", (lease.key.key_id,))
    assert row["cooldown_until"] > time.time() + 60
    assert db.kv_get("learned_rpd:gemini") == 1


def test_auth_failure_kills_key(pool, db):
    lease = pool.acquire("script")
    lease.auth_failed("API key not valid")
    assert db.one("SELECT dead FROM keys WHERE key_id=?", (lease.key.key_id,))["dead"] == 1
    assert all(r["state"] != "ok" for r in pool.status() if r["key_id"] == lease.key.key_id)


def test_no_key_available_reports_retry_time(pool):
    with pytest.raises(NoKeyAvailable) as e:
        for _ in range(10):
            pool.acquire("image", images=1).ok()
    assert e.value.retry_at is not None, "пул должен сказать, когда освободится ключ"


def test_images_quota_per_key(pool, cfg):
    cfg.set("llm.providers.gemini.limits", {"rpm": 100, "rpd": 100, "ipd": 3})
    ids = [pool.acquire("image", images=1).key.key_id for _ in range(6)]   # 2 ключа × ipd 3
    assert len(set(ids)) == 2
    with pytest.raises(NoKeyAvailable):
        pool.acquire("image", images=1)
