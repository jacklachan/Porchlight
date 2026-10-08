"""What a vision provider must return for one porch frame.

The provider is an adviser, not a decision-maker: it reports what it sees and
how sure it is. Whether that reading is allowed to change the care plan is
decided later by the policy gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Reading:
    package_present: bool | None
    person_present: bool | None
    vehicle_present: bool | None
    confidence: float
    summary: str
    provider: str
    model: str | None = None
    error: str | None = None

    @classmethod
    def unknown(cls, provider: str, reason: str, model: str | None = None) -> "Reading":
        return cls(None, None, None, 0.0, "No automatic reading for this frame.", provider, model, reason)


class VisionProvider(Protocol):
    name: str

    async def read(self, image: bytes, content_type: str) -> Reading: ...


class NoVision:
    """No model configured: every frame goes to the family's review queue."""

    name = "none"

    async def read(self, image: bytes, content_type: str) -> Reading:
        return Reading.unknown(self.name, "no vision provider configured")
