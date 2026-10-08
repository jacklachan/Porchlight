from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from porchlight.clock import FixedClock
from porchlight.config import Settings
from porchlight.db import Store
from porchlight.engine import Policy
from porchlight.vision.base import Reading

TZ = "Asia/Kolkata"


def at(hour: int, minute: int = 0, day: int = 12) -> int:
    """Epoch ms for a local time on a fixed Monday (12 Oct 2026)."""
    return int(datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(TZ)).timestamp() * 1000)


def reading(package=None, person=False, vehicle=False, confidence=0.95, error=None) -> Reading:
    return Reading(package, person, vehicle, confidence, "test frame", "test", "test-model", error)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, timezone=TZ, person_name="Mom", vision_provider="fixture")


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(at(8))


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


@pytest.fixture
def alerts_sent() -> list[dict]:
    return []


@pytest.fixture
def policy(store, clock, settings, alerts_sent) -> Policy:
    return Policy(store, clock, settings, notifier=alerts_sent.append)
