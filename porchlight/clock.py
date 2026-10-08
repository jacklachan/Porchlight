"""Clock with an adjustable offset.

Every time-based rule reads the clock through here, so tests and the demo
controls can move time forward without touching the rules themselves.
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS


class Clock:
    def __init__(self, offset_ms: int = 0) -> None:
        self.offset_ms = offset_ms

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms

    def advance(self, ms: int) -> None:
        self.offset_ms += ms

    def reset(self) -> None:
        self.offset_ms = 0


class FixedClock(Clock):
    """A clock that only moves when told to. Used in tests."""

    def __init__(self, start_ms: int) -> None:
        super().__init__(0)
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        self._now += ms

    def set(self, ms: int) -> None:
        self._now = ms


def tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_local(ms: int, tz_name: str) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz(tz_name))


def fmt_time(ms: int, tz_name: str) -> str:
    """'2:14 PM' in the household's timezone."""
    return to_local(ms, tz_name).strftime("%I:%M %p").lstrip("0")


def fmt_duration(ms: int) -> str:
    minutes = max(0, round(ms / MINUTE_MS))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rem = divmod(minutes, 60)
    text = f"{hours} hour{'s' if hours != 1 else ''}"
    if rem >= 5 and hours < 6:
        text += f" {rem} min"
    return text
