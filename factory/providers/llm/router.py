"""LLM-роутер поверх пула ключей.

text()  — один вызов: перебирает ключи по предпочтению провайдеров, сообщает пулу
          исход каждой попытки, ждёт освобождения ключа, падает на llama.cpp.
json()  — text() + извлечение JSON + валидация pydantic; при ошибке схемы
          возвращает модели текст ошибки и просит исправить (до N раз).

Фикстурный генератор используется только в офлайн-режиме. Контент из фикстуры
помечает джоб как synthetic — такой джоб никогда не публикуется на площадки.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ...core.config import Config
from ...core.keypool import KeyPool, NoKeyAvailable
from .base import AuthError, BadRequest, LLMError, LLMResult, Message, ProviderError, RateLimited
from .gemini import GeminiClient
from .openai_compat import OpenAICompatClient

log = logging.getLogger("factory.llm")
T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    """Достаёт JSON из ответа: снимает ```json-ограды, ищет первый сбалансированный объект."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (t.find("{"), t.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("в ответе нет JSON")
    depth, in_str, esc = 0, False, False
    open_ch = t[start]
    close_ch = "}" if open_ch == "{" else "]"
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return json.loads(t[start:i + 1])
    raise ValueError("JSON в ответе обрезан — не хватило max_tokens?")


def _short_errors(e: ValidationError | ValueError, limit: int = 12) -> str:
    if isinstance(e, ValidationError):
        lines = []
        for err in e.errors()[:limit]:
            loc = ".".join(str(x) for x in err.get("loc", ()))
            lines.append(f"- {loc or '(корень)'}: {err.get('msg')}")
        return "\n".join(lines)
    return f"- {e}"


class LLMRouter:
    def __init__(self, cfg: Config, pool: KeyPool | None):
        self.cfg, self.pool = cfg, pool
        self.used_fixture = False
        self.calls: list[dict] = []
        self._broken: set[tuple[str, str]] = set()      # (provider, task) с неверной моделью
        self._clients: dict[str, Any] = {}

    def _client(self, provider: str):
        if provider not in self._clients:
            pc = self.cfg.section(f"llm.providers.{provider}")
            if pc.get("kind") == "gemini":
                self._clients[provider] = GeminiClient(pc["base_url"])
            else:
                self._clients[provider] = OpenAICompatClient(provider, pc["base_url"])
        return self._clients[provider]

    # ------------------------------------------------------------------ text
    def text(self, task_class: str, system: str, messages: list[Message], *, json_mode: bool = False,
             max_tokens: int = 8192, temperature: float = 0.8,
             fixture: Callable[[], str] | None = None) -> LLMResult:
        if self.cfg.offline:
            return self._offline(task_class, system, messages, json_mode, max_tokens, temperature, fixture)
        if self.pool is None or not self.pool.keys:
            log.warning("Пул ключей пуст — пробую локальный llama.cpp")
            return self._llamacpp_or_raise(task_class, system, messages, json_mode, max_tokens, temperature)

        est = int(sum(len(m.content) for m in messages) / 3) + max_tokens // 2
        attempts = int(self.cfg.get("llm.max_attempts_per_call", 12))
        wait_budget = float(self.cfg.get("llm.wait_for_cooldown_s", 90))
        excluded: set[str] = set()
        last_err: Exception | None = None
        for _ in range(attempts):
            try:
                lease = self.pool.acquire(task_class, est_tokens=est, exclude=excluded)
            except NoKeyAvailable as e:
                if e.retry_at and e.retry_at - time.time() <= wait_budget:
                    pause = max(1.0, e.retry_at - time.time())
                    wait_budget -= pause
                    log.info("Все ключи для %s заняты, жду %.0f с", task_class, pause)
                    time.sleep(pause)
                    continue
                last_err = e
                break
            if (lease.provider, task_class) in self._broken:
                lease.ok(0)
                excluded.add(lease.key.key_id)
                continue
            t0 = time.time()
            try:
                res = self._client(lease.provider).complete(
                    system=system, messages=messages, model=lease.model, api_key=lease.key.secret,
                    json_mode=json_mode, max_tokens=max_tokens, temperature=temperature)
                lease.ok(res.tokens)
                res.key_id = lease.key.key_id
                self.calls.append({"task": task_class, "provider": res.provider, "model": res.model,
                                   "tokens": res.tokens, "s": round(time.time() - t0, 1)})
                return res
            except RateLimited as e:
                lease.rate_limited(e.retry_after, e.daily, str(e))
                last_err = e
            except AuthError as e:
                lease.auth_failed(str(e))
                last_err = e
            except BadRequest as e:
                lease.ok(0)
                log.error("Провайдер %s отверг запрос %s (модель %s): %s", lease.provider, task_class,
                          lease.model, e)
                self._broken.add((lease.provider, task_class))
                last_err = e
            except (ProviderError, httpx.HTTPError) as e:
                lease.failed(str(e))
                last_err = e
            excluded.add(lease.key.key_id)
        log.warning("Облачные ключи для %s исчерпаны (%s) — пробую llama.cpp", task_class, last_err)
        return self._llamacpp_or_raise(task_class, system, messages, json_mode, max_tokens, temperature,
                                       cause=last_err)

    def _llamacpp(self, system, messages, json_mode, max_tokens, temperature) -> LLMResult:
        lc = self.cfg.section("llm.llamacpp")
        client = OpenAICompatClient("llamacpp", lc.get("base_url", "http://127.0.0.1:8080/v1"),
                                    timeout=float(lc.get("timeout_s", 900)))
        return client.complete(system=system, messages=messages, model=lc.get("model", "local"), api_key="",
                               json_mode=json_mode, max_tokens=min(max_tokens, 8192), temperature=temperature)

    def _llamacpp_or_raise(self, task_class, system, messages, json_mode, max_tokens, temperature,
                           cause: Exception | None = None) -> LLMResult:
        if self.cfg.get("llm.offline_fallback", "llamacpp") == "llamacpp":
            try:
                res = self._llamacpp(system, messages, json_mode, max_tokens, temperature)
                self.calls.append({"task": task_class, "provider": "llamacpp", "model": res.model,
                                   "tokens": res.tokens})
                return res
            except (LLMError, httpx.HTTPError) as e:
                log.warning("llama.cpp недоступен: %s", e)
        raise LLMUnavailable(f"Нет доступного LLM для {task_class}: {cause}")

    def _offline(self, task_class, system, messages, json_mode, max_tokens, temperature, fixture) -> LLMResult:
        try:
            res = self._llamacpp(system, messages, json_mode, max_tokens, temperature)
            self.calls.append({"task": task_class, "provider": "llamacpp", "model": res.model})
            return res
        except (LLMError, httpx.HTTPError):
            pass
        if fixture is None:
            raise LLMUnavailable(f"Офлайн: для {task_class} нет ни llama.cpp, ни фикстуры")
        self.used_fixture = True
        self.calls.append({"task": task_class, "provider": "fixture", "model": "offline-fixture"})
        return LLMResult(text=fixture(), tokens=0, provider="fixture", model="offline-fixture")

    # ------------------------------------------------------------------ json
    def json(self, task_class: str, system: str, user: str, model: type[T] | None = None, *,
             validate: Callable[[Any], list[str]] | None = None,
             prepare: Callable[[Any], Any] | None = None, max_tokens: int = 8192,
             temperature: float = 0.8, fixture: Callable[[], Any] | None = None) -> Any:
        """Вызов с валидацией. validate() — доп. проверки поверх схемы (драматургия, инварианты)."""
        messages = [Message("user", user)]
        retries = int(self.cfg.get("llm.validation_retries", 3))
        fixture_text = (lambda: json.dumps(fixture(), ensure_ascii=False)) if fixture else None
        last: str = ""
        for attempt in range(retries + 1):
            res = self.text(task_class, system, messages, json_mode=True, max_tokens=max_tokens,
                            temperature=temperature, fixture=fixture_text)
            try:
                data = extract_json(res.text)
                if prepare:
                    data = prepare(data)
                obj = model.model_validate(data) if model else data
                problems = validate(obj) if validate else []
                if not problems:
                    return obj
                last = "\n".join(f"- {p}" for p in problems)
            except (ValidationError, ValueError) as e:
                last = _short_errors(e)
            if res.provider == "fixture":
                raise LLMUnavailable(f"Фикстура {task_class} не прошла проверку:\n{last}")
            log.info("%s: ответ не прошёл проверку (попытка %d/%d):\n%s", task_class, attempt + 1, retries + 1,
                     last)
            messages = messages + [
                Message("assistant", res.text[:12000]),
                Message("user", "Ответ не прошёл автоматическую проверку:\n" + last +
                        "\nИсправь перечисленное и верни ПОЛНЫЙ исправленный JSON целиком, без пояснений."),
            ]
        raise LLMUnavailable(f"{task_class}: модель не выдала валидный ответ за {retries + 1} попыток:\n{last}")
