"""Google Gemini API (free tier): текст, JSON-режим, изображения (Nano Banana), эмбеддинги."""
from __future__ import annotations

import base64

import httpx

from .base import BadRequest, LLMResult, Message, ProviderError, raise_for_llm_status


class GeminiClient:
    def __init__(self, base_url: str, timeout: float = 180):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, model: str, method: str, api_key: str, body: dict) -> dict:
        url = f"{self.base_url}/models/{model}:{method}"
        try:
            resp = httpx.post(url, params={"key": api_key}, json=body, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise ProviderError(f"gemini сеть: {e}") from e
        raise_for_llm_status(resp, "gemini")
        return resp.json()

    def complete(self, *, system: str, messages: list[Message], model: str, api_key: str,
                 json_mode: bool, max_tokens: int, temperature: float) -> LLMResult:
        contents = [{"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
                    for m in messages]
        gen: dict = {"maxOutputTokens": max_tokens, "temperature": temperature}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body = {"contents": contents, "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        data = self._post(model, "generateContent", api_key, body)
        cands = data.get("candidates") or []
        if not cands:
            raise BadRequest(f"gemini: пустой ответ (promptFeedback={data.get('promptFeedback')})")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise ProviderError(f"gemini: нет текста, finishReason={cands[0].get('finishReason')}")
        tokens = int((data.get("usageMetadata") or {}).get("totalTokenCount", 0))
        return LLMResult(text=text, tokens=tokens, provider="gemini", model=model)

    def image(self, *, prompt: str, model: str, api_key: str, aspect_ratio: str = "16:9") -> tuple[bytes, str]:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": aspect_ratio}},
        }
        data = self._post(model, "generateContent", api_key, body)
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"]), inline.get("mimeType", "image/png")
        raise BadRequest("gemini image: в ответе нет изображения (возможно, сработал фильтр безопасности)")

    def embed(self, *, text: str, model: str, api_key: str) -> list[float]:
        data = self._post(model, "embedContent", api_key, {"content": {"parts": [{"text": text[:8000]}]}})
        return list((data.get("embedding") or {}).get("values") or [])
