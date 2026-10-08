#!/usr/bin/env python3
"""Read one image with the configured vision provider and print the result.

    python scripts/vision_check.py data/media/<frame>.jpg

Uses the same settings as the server (.env: VISION_PROVIDER, BEDROCK_MODEL_ID, AWS_REGION and
AWS credentials), so it is the quickest way to confirm Bedrock works before starting Porchlight.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porchlight.config import Settings, load_dotenv  # noqa: E402
from porchlight.vision import build_vision  # noqa: E402


async def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    load_dotenv()
    settings = Settings.from_env()
    vision = build_vision(settings)
    model = getattr(vision, "model_id", None) or getattr(vision, "model", None) or "-"
    print(f"provider: {vision.name}   model: {model}")
    content_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    started = time.time()
    reading = await vision.read(path.read_bytes(), content_type)
    print(f"took {time.time() - started:.1f}s")
    if reading.error:
        print(f"FAILED: {reading.error}")
        return 1
    print(f"package: {reading.package_present}   person: {reading.person_present}   vehicle: {reading.vehicle_present}")
    print(f"confidence: {reading.confidence:.2f}   (acts without a person at {settings.confidence_threshold:.2f} or above)")
    print(f"summary: {reading.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
