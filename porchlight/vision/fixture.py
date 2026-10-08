"""Reads the ground-truth labels the local stand-in embeds in its own frames.

Only useful with porchlight.ring.simulator. On a real Ring frame it reports
"unknown", which sends the frame to human review rather than guessing.
"""

from __future__ import annotations

from ..ring.simulator import read_labels
from .base import Reading


class FixtureVision:
    name = "fixture"

    async def read(self, image: bytes, content_type: str) -> Reading:
        labels = read_labels(image)
        if labels is None:
            return Reading.unknown(self.name, "frame did not come from the local stand-in")
        return Reading(
            package_present=bool(labels["package"]),
            person_present=bool(labels["person"]),
            vehicle_present=bool(labels["vehicle"]),
            confidence=0.99,
            summary=labels.get("summary", ""),
            provider=self.name,
            model="embedded-labels",
        )
