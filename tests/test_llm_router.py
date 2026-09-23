import pytest
from pydantic import BaseModel

from factory.core.keypool import ApiKey, KeyPool
from factory.providers.llm.base import BadRequest, LLMResult, RateLimited
from factory.providers.llm.router import LLMRouter, extract_json


class Tiny(BaseModel):
    value: int


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(text=item, tokens=5, provider="x", model=kw["model"])


@pytest.fixture
def router(cfg, db):
    cfg.set("runtime.offline", False)
    cfg.set("llm.offline_fallback", "none")
    pool = KeyPool(db, cfg, [ApiKey("gemini", "a1", "k1"), ApiKey("gemini", "a2", "k2"), ApiKey("groq", "a1", "k3")])
    return LLMRouter(cfg, pool)


def test_rotates_key_after_rate_limit(router, monkeypatch):
    fake = FakeClient([RateLimited("429", 30), '{"value": 1}'])
    monkeypatch.setattr(router, "_client", lambda p: fake)
    res = router.text("script", "", [], json_mode=True)
    assert res.text == '{"value": 1}'
    keys = [c["api_key"] for c in fake.calls]
    assert keys[0] != keys[1]


def test_bad_model_skips_provider_for_task(router, monkeypatch):
    fake = FakeClient([BadRequest("404 model not found"), '{"value": 2}'])
    monkeypatch.setattr(router, "_client", lambda p: fake)
    router.text("script", "", [])
    assert ("gemini", "script") in router._broken
    assert fake.calls[1]["model"] == router.pool.model_for("groq", "script")


def test_json_validation_error_is_returned_to_model(router, monkeypatch):
    fake = FakeClient(['{"value": "не число"}', '```json\n{"value": 3}\n```'])
    monkeypatch.setattr(router, "_client", lambda p: fake)
    obj = router.json("script", "sys", "дай число", Tiny)
    assert obj.value == 3
    second = fake.calls[1]["messages"]
    assert any("не прошёл автоматическую проверку" in m.content for m in second)


def test_extra_validation_hook(router, monkeypatch):
    fake = FakeClient(['{"value": 1}', '{"value": 10}'])
    monkeypatch.setattr(router, "_client", lambda p: fake)
    obj = router.json("script", "", "", Tiny, validate=lambda o: [] if o.value > 5 else ["слишком мало"])
    assert obj.value == 10


@pytest.mark.parametrize("text", ['{"a": 1}', 'Вот ответ: {"a": 1} — готово', '```json\n{"a": 1}\n```',
                                  'текст {"a": {"b": "}"}, "c": 1} хвост'])
def test_extract_json(text):
    assert "a" in extract_json(text)


def test_offline_uses_fixture_and_flags_synthetic(cfg, db):
    cfg.set("runtime.offline", True)
    r = LLMRouter(cfg, None)
    r._llamacpp = lambda *a, **k: (_ for _ in ()).throw(BadRequest("нет сервера"))
    assert r.json("script", "", "", Tiny, fixture=lambda: {"value": 7}).value == 7
    assert r.used_fixture
