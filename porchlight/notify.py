"""Outbound notifications: one optional generic webhook.

Porchlight POSTs alerts (attention and urgent) and approved actions as JSON to
NOTIFY_WEBHOOK_URL. Point it at whatever the family already uses: an ntfy
topic, a Slack or Telegram relay, an SMS gateway.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("porchlight.notify")


class WebhookNotifier:
    def __init__(self, url: str | None) -> None:
        self.url = url
        self._tasks: set[asyncio.Task[None]] = set()

    def __call__(self, payload: dict[str, Any]) -> str | None:
        if not self.url:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(self._post(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return "Passed to the family's notification webhook."

    async def _post(self, payload: dict[str, Any]) -> None:
        body = {
            "source": "porchlight",
            "type": payload.get("type"),
            "level": payload.get("level"),
            "title": payload.get("title"),
            "body": payload.get("body") or payload.get("detail"),
            "id": payload.get("id"),
            "contact": payload.get("contact"),
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.url, json=body)  # type: ignore[arg-type]
            if resp.status_code >= 400:
                log.warning("notification webhook answered %s", resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("notification webhook failed: %s", exc)
