"""Общие типы LLM-провайдеров и разбор ошибок HTTP в понятные пулу исходы."""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx


class LLMError(Exception):
    pass


class RateLimited(LLMError):
    def __init__(self, message: str, retry_after: float | None = None, daily: bool = False):
        super().__init__(message)
        self.retry_after, self.daily = retry_after, daily


class AuthError(LLMError):
    pass


class BadRequest(LLMError):
    """Проблема запроса/модели, а не ключа: 404 model not found, неверный параметр."""


class ProviderError(LLMError):
    """5xx, таймаут, сеть — временная проблема."""


@dataclass
class LLMResult:
    text: str
    tokens: int
    provider: str
    model: str
    key_id: str = ""


@dataclass
class Message:
    role: str      # user | assistant
    content: str


DAILY_MARKERS = ("perday", "per day", "per_day", "rpd", "tpd", "daily", "free-models-per-day", "quota exceeded for quota metric")


def parse_retry_after(resp: httpx.Response) -> float | None:
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', resp.text)
    if m:
        return float(m.group(1))
    m = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", resp.text)
    if m:
        return float(m.group(1) or 0) * 60 + float(m.group(2))
    return None


def raise_for_llm_status(resp: httpx.Response, provider: str) -> None:
    code = resp.status_code
    if code < 400:
        return
    body = resp.text[:800]
    low = body.lower()
    if code == 429 or (code == 402 and provider == "openrouter"):
        daily = code == 402 or any(m in low for m in DAILY_MARKERS)
        raise RateLimited(f"{provider} {code}: {body[:200]}", parse_retry_after(resp), daily)
    if code in (401, 403) or "api key not valid" in low or "invalid api key" in low:
        raise AuthError(f"{provider} {code}: {body[:200]}")
    if code in (400, 404, 422):
        raise BadRequest(f"{provider} {code}: {body[:300]}")
    raise ProviderError(f"{provider} {code}: {body[:200]}")
