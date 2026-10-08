"""Porch frame reading with Amazon Bedrock (Converse API).

The model is forced to answer through a single tool call, so the output is a
fixed JSON shape rather than prose we would have to parse.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .base import Reading

PROMPT = (
    "This is one frame from a doorbell camera looking at a home's front porch. "
    "Report only what is visible in this frame. Do not identify anyone and do not describe "
    "faces, age, gender or ethnicity.\n"
    "- package_present: a parcel, box, bag or envelope has been left on the porch, step or doormat.\n"
    "- person_present: at least one person is in view.\n"
    "- vehicle_present: a car, van, truck or motorbike is in view.\n"
    "- confidence: 0 to 1, how sure you are about package_present specifically. "
    "Use a low value if the frame is dark, blurred, blocked, or the object is ambiguous.\n"
    "- summary: one plain sentence a family member could read, under 25 words."
)

TOOL = {
    "toolSpec": {
        "name": "report_porch",
        "description": "Report what is visible in the porch frame.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "package_present": {"type": "boolean"},
                    "person_present": {"type": "boolean"},
                    "vehicle_present": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "summary": {"type": "string"},
                },
                "required": ["package_present", "person_present", "vehicle_present", "confidence", "summary"],
            }
        },
    }
}

_FORMATS = {"image/jpeg": "jpeg", "image/jpg": "jpeg", "image/png": "png", "image/webp": "webp"}


def parse_tool_output(response: dict[str, Any], provider: str, model: str) -> Reading:
    """Pull the report_porch tool input out of a Converse response."""
    blocks = ((response.get("output") or {}).get("message") or {}).get("content") or []
    for block in blocks:
        tool_use = block.get("toolUse")
        if not tool_use or tool_use.get("name") != "report_porch":
            continue
        data = tool_use.get("input") or {}
        try:
            confidence = max(0.0, min(1.0, float(data["confidence"])))
            return Reading(
                package_present=bool(data["package_present"]),
                person_present=bool(data["person_present"]),
                vehicle_present=bool(data["vehicle_present"]),
                confidence=confidence,
                summary=str(data.get("summary", "")).strip()[:240],
                provider=provider,
                model=model,
            )
        except (KeyError, TypeError, ValueError):
            break
    return Reading.unknown(provider, "model did not return a usable report", model)


class BedrockVision:
    name = "bedrock"

    def __init__(self, model_id: str, region: str, client: Any | None = None) -> None:
        self.model_id = model_id
        self.region = region
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # optional dependency: pip install "porchlight[aws]"

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def _converse(self, image: bytes, fmt: str) -> dict[str, Any]:
        return self._get_client().converse(
            modelId=self.model_id,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"image": {"format": fmt, "source": {"bytes": image}}},
                        {"text": PROMPT},
                    ],
                }
            ],
            toolConfig={"tools": [TOOL], "toolChoice": {"tool": {"name": "report_porch"}}},
            inferenceConfig={"maxTokens": 300, "temperature": 0},
        )

    async def read(self, image: bytes, content_type: str) -> Reading:
        fmt = _FORMATS.get(content_type.split(";")[0].strip().lower(), "jpeg")
        try:
            response = await asyncio.to_thread(self._converse, image, fmt)
        except Exception as exc:  # boto3 raises many types; any failure means "no reading"
            return Reading.unknown(self.name, f"{type(exc).__name__}: {exc}"[:300], self.model_id)
        return parse_tool_output(response, self.name, self.model_id)
