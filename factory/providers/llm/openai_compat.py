"""OpenAI-совместимые API: Groq, OpenRouter, локальный llama.cpp (llama-server)."""
from __future__ import annotations

import httpx

from .base import BadRequest, LLMResult, Message, ProviderError, raise_for_llm_status


class OpenAICompatClient:
    def __init__(self, provider: str, base_url: str, timeout: float = 180):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, *, system: str, messages: list[Message], model: str, api_key: str,
                 json_mode: bool, max_tokens: int, temperature: float) -> LLMResult:
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": m.role, "content": m.content} for m in messages]
        body: dict = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        if self.provider == "openrouter":
            headers.update({"HTTP-Referer": "https://github.com/akikat1/Contentzavod", "X-Title": "contentzavod"})
        try:
            resp = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout)
            if resp.status_code == 400 and json_mode and "response_format" in resp.text:
                body.pop("response_format")       # модель не умеет JSON-режим — просим текстом
                resp = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers,
                                  timeout=self.timeout)
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.provider} сеть: {e}") from e
        raise_for_llm_status(resp, self.provider)
        data = resp.json()
        if data.get("error"):
            raise ProviderError(f"{self.provider}: {data['error']}")
        choices = data.get("choices") or []
        if not choices:
            raise BadRequest(f"{self.provider}: пустой ответ")
        text = (choices[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise ProviderError(f"{self.provider}: пустой текст, finish={choices[0].get('finish_reason')}")
        tokens = int((data.get("usage") or {}).get("total_tokens", 0))
        return LLMResult(text=text, tokens=tokens, provider=self.provider, model=model)
