"""Porch frame reading through any OpenAI-compatible chat completions endpoint.

Bedrock is the default, but new AWS accounts sometimes start with zero Bedrock
quota. This provider keeps frame reading working with whatever vision model is
available: a hosted OpenAI-compatible API, or a local one such as Ollama
(VISION_BASE_URL=http://localhost:11434/v1).
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from .base import Reading
from .bedrock import PROMPT

JSON_INSTRUCTION = (
    "\nAnswer with one JSON object and nothing else, with exactly these keys: "
    '{"package_present": true|false, "person_present": true|false, "vehicle_present": true|false, '
    '"confidence": 0.0-1.0, "summary": "one sentence"}'
)


def parse_json_reply(text: str, provider: str, model: str) -> Reading:
    """Models wrap JSON in prose or code fences often enough that we look for the object."""
    match = re.search(r"\{.*\}", text or "", flags=re.S)
    if not match:
        return Reading.unknown(provider, "model did not return JSON", model)
    try:
        data = json.loads(match.group(0))
        confidence = max(0.0, min(1.0, float(data["confidence"])))
        flags = [data["package_present"], data["person_present"], data["vehicle_present"]]
        if not all(isinstance(flag, bool) for flag in flags):
            raise TypeError("flags must be booleans")
    except (ValueError, KeyError, TypeError):
        return Reading.unknown(provider, "model returned JSON in the wrong shape", model)
    return Reading(
        package_present=flags[0],
        person_present=flags[1],
        vehicle_present=flags[2],
        confidence=confidence,
        summary=str(data.get("summary", "")).strip()[:240],
        provider=provider,
        model=model,
    )


class OpenAICompatVision:
    name = "openai"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._transport = transport

    async def read(self, image: bytes, content_type: str) -> Reading:
        mime = content_type.split(";")[0].strip() or "image/jpeg"
        data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 1200,  # room for models that think before answering
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT + JSON_INSTRUCTION},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=60, transport=self._transport) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            if resp.status_code != 200:
                return Reading.unknown(self.name, f"HTTP {resp.status_code}: {resp.text[:200]}", self.model)
            content = resp.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            return Reading.unknown(self.name, f"{type(exc).__name__}: {exc}"[:300], self.model)
        if isinstance(content, list):  # some servers return content parts
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        return parse_json_reply(content, self.name, self.model)
