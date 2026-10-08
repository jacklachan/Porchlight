"""From Ring to the policy gate: events in, frames fetched, readings recorded.

Three ways an event reaches Porchlight:

* a signed webhook (motion_detected, button_press) from Ring,
* polling the Event History API, which is what works with a Playground token,
* a frame captured in the browser from the WHEP live view.

Each ends the same way: store the raw event, fetch or accept one frame, ask
the vision provider what it shows, hand the reading to the policy gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import httpx

from ..clock import MINUTE_MS, Clock
from ..config import Settings
from ..db import Store
from ..ring import HistoryEvent, MediaNotReady, RingAuthError, RingClient, RingError, WebhookEvent
from ..vision.base import VisionProvider
from .policy import Policy

log = logging.getLogger("porchlight.ingest")

# A webhook and a history record describe the same moment under different ids.
SAME_MOMENT_MS = 8_000
_HISTORY_TO_TYPE = {"motion": "motion", "ding": "ding", "on_demand": "on_demand"}
_WEBHOOK_TO_TYPE = {"motion_detected": "motion", "button_press": "ding"}
_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp"}


class Ingest:
    def __init__(
        self,
        store: Store,
        clock: Clock,
        settings: Settings,
        ring: RingClient,
        vision: VisionProvider,
        policy: Policy,
    ) -> None:
        self.store = store
        self.clock = clock
        self.settings = settings
        self.ring = ring
        self.vision = vision
        self.policy = policy
        self.device_id: str | None = settings.ring_device_id
        self.device_name: str | None = None
        self.last_error: str | None = None
        self.last_poll_ms: int | None = None
        self._last_porch_check_ms = 0
        self._lock = asyncio.Lock()
        self._listeners: set[asyncio.Queue[str]] = set()
        settings.media_dir.mkdir(parents=True, exist_ok=True)

    # -- change notifications for the dashboard --------------------------

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=20)
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._listeners.discard(queue)

    def changed(self, what: str = "state") -> None:
        for queue in list(self._listeners):
            try:
                queue.put_nowait(what)
            except asyncio.QueueFull:
                pass

    # -- ring connection ---------------------------------------------------

    async def ensure_device(self) -> str:
        if self.device_id and self.device_name:
            return self.device_id
        devices = await self.ring.list_devices()
        if not devices:
            raise RingError("The Ring account has no devices shared with this app")
        chosen = next((d for d in devices if d.id == self.device_id), None) if self.device_id else None
        if chosen is None:
            # Prefer a doorbell: the porch is what we watch.
            chosen = next((d for d in devices if "doorbell" in f"{d.kind} {d.name}".lower()), devices[0])
        self.device_id, self.device_name = chosen.id, chosen.name
        return chosen.id

    def connection(self) -> dict[str, Any]:
        return {
            "auth_mode": self.ring.auth_mode,
            "api_base": self.ring.api_base,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "last_poll": self.last_poll_ms,
            "last_error": self.last_error,
            "simulated": "/sim" in self.ring.api_base,
        }

    # -- events ------------------------------------------------------------

    def _store_event(
        self,
        *,
        event_id: str,
        device_id: str,
        type_: str,
        sub_type: str | None,
        ts: int,
        end_ts: int | None,
        source: str,
        raw: dict[str, Any],
    ) -> bool:
        """Insert the event unless we already hold this moment from the other source."""
        if self.store.get("events", event_id):
            return False
        twin = self.store.one(
            "SELECT id FROM events WHERE device_id = ? AND type = ? AND source != ? AND ABS(ts - ?) <= ?",
            [device_id, type_, source, ts, SAME_MOMENT_MS],
        )
        if twin:
            return False
        return self.store.insert(
            "events",
            {
                "id": event_id,
                "device_id": device_id,
                "type": type_,
                "sub_type": sub_type,
                "ts": ts,
                "end_ts": end_ts,
                "source": source,
                "raw": raw,
                "created_at": self.clock.now_ms(),
            },
            ignore=True,
        )

    def accept_webhook(self, event: WebhookEvent) -> str:
        """Record a verified webhook. Returns 'duplicate', 'ignored' or 'stored'."""
        fresh = self.store.insert(
            "webhook_requests", {"request_id": event.request_id, "ts": self.clock.now_ms()}, ignore=True
        )
        if not fresh:
            return "duplicate"
        if not event.is_device_event or not event.device_id or event.timestamp_ms is None:
            self.store.record(self.clock.now_ms(), "ring", event.event_id, f"webhook {event.type} received")
            return "ignored"
        stored = self._store_event(
            event_id=event.event_id,
            device_id=event.device_id,
            type_=_WEBHOOK_TO_TYPE[event.type],
            sub_type=event.sub_type,
            ts=event.timestamp_ms,
            end_ts=None,
            source="webhook",
            raw=event.raw,
        )
        return "stored" if stored else "duplicate"

    async def poll_history(self) -> int:
        """Pull new events from Event History. Returns how many were new."""
        device_id = await self.ensure_device()
        history = await self.ring.event_history(device_id, max_pages=2)
        self.last_poll_ms = self.clock.now_ms()
        cutoff = int(self.store.get_meta("history_cutoff", "0") or 0)
        new_events: list[HistoryEvent] = []
        for item in history:
            if item.start_ms <= cutoff:
                continue
            stored = self._store_event(
                event_id=item.id,
                device_id=item.device_id,
                type_=_HISTORY_TO_TYPE.get(item.event_type.split(".")[0], item.event_type),
                sub_type=item.event_type.split(".", 1)[1] if "." in item.event_type else None,
                ts=item.start_ms,
                end_ts=item.end_ms,
                source="history",
                raw=item.raw,
            )
            if stored:
                new_events.append(item)
        for item in sorted(new_events, key=lambda e: e.start_ms):
            await self.observe_event(item.id)
        return len(new_events)

    def mark_history_cutoff(self) -> None:
        """Ignore history older than now: on first connect we do not replay yesterday."""
        self.store.set_meta("history_cutoff", str(self.clock.now_ms()))

    # -- frames ------------------------------------------------------------

    def _snapshot_source(self) -> str:
        """Stand-in frames are never labelled as Ring footage."""
        return "stand_in_snapshot" if "/sim" in self.ring.api_base else "ring_snapshot"

    def _save_frame(self, content: bytes, content_type: str) -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        ext = _EXT.get(content_type.split(";")[0].strip().lower(), "jpg")
        name = f"{digest[:24]}.{ext}"
        path = self.settings.media_dir / name
        if not path.exists():
            path.write_bytes(content)
        return name, digest

    async def observe_event(self, event_id: str) -> dict[str, Any] | None:
        """Fetch the frame for a stored event and record what it shows."""
        event = self.store.get("events", event_id)
        if not event:
            return None
        if self.store.one("SELECT id FROM observations WHERE event_id = ?", [event_id]):
            return None
        try:
            media = await self._frame_near(event["device_id"], event["ts"])
        except MediaNotReady as exc:
            self.store.record(
                self.clock.now_ms(), "ring", event_id, "no frame available for event", detail={"error": str(exc)}
            )
            await self.evaluate()
            return None
        return await self.observe_frame(
            media.content,
            media.content_type,
            ts=event["ts"],
            frame_source=self._snapshot_source(),
            event_id=event_id,
            device_id=event["device_id"],
        )

    async def _frame_near(self, device_id: str, ts: int) -> Any:
        """The snapshot at the event time, or failing that the latest one just after it."""
        now = self.clock.now_ms()
        try:
            return await self.ring.snapshot_at(device_id, min(ts, now))
        except MediaNotReady:
            return await self.ring.latest_snapshot(device_id, ts - MINUTE_MS, min(ts + 2 * MINUTE_MS, now))

    async def observe_frame(
        self,
        content: bytes,
        content_type: str,
        *,
        ts: int,
        frame_source: str,
        event_id: str | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        name, digest = self._save_frame(content, content_type)
        reading = await self.vision.read(content, content_type)
        async with self._lock:
            obs = self.policy.record_observation(
                reading,
                ts=ts,
                frame_source=frame_source,
                event_id=event_id,
                device_id=device_id or self.device_id,
                snapshot_file=name,
                snapshot_sha256=digest,
            )
        await self.evaluate()
        return obs

    async def check_porch_now(self) -> dict[str, Any]:
        """Ask Ring for the most recent frame and read it, without waiting for motion."""
        device_id = await self.ensure_device()
        now = self.clock.now_ms()
        self._last_porch_check_ms = now
        try:
            media = await self.ring.latest_snapshot(device_id, now - 10 * MINUTE_MS, now)
        except MediaNotReady:
            # Ring only serves media for times the app is authorized for; try just the last minute.
            media = await self.ring.latest_snapshot(device_id, now - MINUTE_MS, now)
        return await self.observe_frame(
            media.content, media.content_type, ts=now, frame_source=self._snapshot_source(), device_id=device_id
        )

    # -- evaluation and the background loop ----------------------------------

    async def evaluate(self) -> list[dict[str, Any]]:
        async with self._lock:
            alerts = self.policy.evaluate()
        self.changed()
        return alerts

    async def tick(self) -> None:
        """One pass of the background loop."""
        try:
            if self.ring.auth_mode:
                await self.poll_history()
                due = self.clock.now_ms() - self._last_porch_check_ms >= self.settings.porch_check_minutes * MINUTE_MS
                if due and self.policy.needs_porch_check():
                    try:
                        await self.check_porch_now()
                    except MediaNotReady:
                        self._last_porch_check_ms = self.clock.now_ms()
            self.last_error = None
        except RingAuthError as exc:
            self.last_error = f"Ring token rejected or missing: {exc}"
        except (RingError, httpx.HTTPError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("ring poll failed: %s", exc)
        await self.evaluate()

    async def run(self) -> None:
        if not self.store.get_meta("history_cutoff"):
            self.mark_history_cutoff()
        while True:
            try:
                await self.tick()
            except Exception:  # keep the loop alive; the error is logged with its traceback
                log.exception("ingest tick failed")
            await asyncio.sleep(self.settings.poll_seconds)
