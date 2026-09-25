from __future__ import annotations

import json
from typing import Any, Protocol

import httpx2


class StructuredModelClient(Protocol):
    async def generate_json(
        self,
        *,
        model_id: str,
        system_prompt: str,
        payload: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]: ...


class OpenAICompatibleModelClient:
    def __init__(self, base_url: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def generate_json(
        self,
        *,
        model_id: str,
        system_prompt: str,
        payload: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        request = {
            "model": model_id,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
        }
        async with httpx2.AsyncClient(timeout=90.0, headers=headers) as client:
            response = await client.post(f"{self.base_url}/chat/completions", json=request)
            response.raise_for_status()
            body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
            value = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("model did not return one JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("model response must be a JSON object")
        return value
