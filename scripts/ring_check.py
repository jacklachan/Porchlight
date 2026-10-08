#!/usr/bin/env python3
"""Check a Ring token against the real Ring Partner API, one capability at a time.

    python scripts/ring_check.py --token "eyJ..."          # or set RING_ACCESS_TOKEN

Prints which of the calls Porchlight depends on work with this token, and saves
any frame it gets to ring_check_frame.jpg. Run it first whenever you paste a new
Playground token: it tells you in a few seconds whether snapshots, event
history and live view are available before you start recording a demo.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porchlight.config import RING_API_BASE  # noqa: E402
from porchlight.ring import MediaNotReady, RingClient, RingError  # noqa: E402



async def step(label: str, coro):
    try:
        result = await coro
        print(f"  ok    {label}")
        return result
    except MediaNotReady as exc:
        print(f"  none  {label}: {exc}")
    except RingError as exc:
        print(f"  FAIL  {label}: {exc}")
    except Exception as exc:  # network errors and the like
        print(f"  FAIL  {label}: {type(exc).__name__}: {exc}")
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--token", default=os.environ.get("RING_ACCESS_TOKEN"))
    parser.add_argument("--api-base", default=os.environ.get("RING_API_BASE", RING_API_BASE))
    parser.add_argument("--device-id")
    args = parser.parse_args()
    if not args.token:
        parser.error("pass --token or set RING_ACCESS_TOKEN")

    ring = RingClient(access_token=args.token, api_base=args.api_base)
    print(f"Ring Partner API at {ring.api_base}")
    try:
        devices = await step("list devices (GET /v1/devices)", ring.list_devices())
        if not devices:
            print("\nNo devices: nothing else can be checked. Is the token fresh? Playground tokens last about 30 minutes.")
            return 1
        for d in devices:
            print(f"          {d.id}  {d.name}  ({d.kind})")
        device_id = args.device_id or devices[0].id

        status = await step("device status", ring.device_status(device_id))
        if status is not None:
            print(f"          {status}")
        await step("device capabilities", ring.device_capabilities(device_id))

        history = await step("event history (GET /v1/history/devices/{id}/events)", ring.event_history(device_id))
        if history is not None:
            print(f"          {len(history)} event(s)")
            for event in history[:5]:
                print(f"          {event.event_type:<12} start={event.start_ms} id={event.id}")

        now = int(time.time() * 1000)
        frame = await step(
            "latest snapshot in the last 10 minutes (image download, latest_in_range)",
            ring.latest_snapshot(device_id, now - 10 * 60_000, now),
        )
        if frame is None and history:
            frame = await step(
                "snapshot at the newest event (image download, at_timestamp)",
                ring.snapshot_at(device_id, history[0].start_ms),
            )
        if frame is not None:
            Path("ring_check_frame.jpg").write_bytes(frame.content)
            print(f"          saved ring_check_frame.jpg ({len(frame.content)} bytes, {frame.content_type})")

        print("  ....  live view needs a browser; open the dashboard and press Live view.")
        print(
            "\nIf snapshots show 'none', use Live view in the dashboard and press 'Read this frame': "
            "that path does not depend on stored media."
        )
    finally:
        await ring.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
