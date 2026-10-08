"""Porchlight's MCP server (Streamable HTTP), the surface an assistant such as Alexa+ talks to.

Tools read the care plan and its evidence, add check-ins, and queue requests
for the family. Two rules shape the tool list:

* Reading is free, acting is gated. ``propose_action`` only queues a request.
  ``decide_action`` is registered with MCP Apps visibility ``["app"]``: it is
  callable from the card a person taps, and is never offered to the model.
* Answers carry their evidence. Tools that make a claim about the porch return
  the frame they rest on as image content, plus the rule that fired.
"""

from __future__ import annotations

import base64
import io
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.apps import Apps
from mcp.server.mcpserver import MCPServer
from PIL import Image

from . import __version__
from .clock import DAY_MS, fmt_time, to_local, tz
from .engine import RULES, Ingest, Policy
from .ring import RingError

CARD_URI = "ui://porchlight/card.html"
WEB_DIR = Path(__file__).parent / "web"

INSTRUCTIONS = (
    "Porchlight watches the front door of a relative who lives alone and checks that expected "
    "deliveries and visits actually happen. Use porch_status for 'how is she / did it arrive' questions. "
    "Use get_evidence before stating why an alert was raised. You cannot carry out actions: "
    "propose_action queues a request that a family member must approve on screen. "
    "Never describe or identify people in frames beyond 'a person'."
)

READ_ONLY = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


def _text(text: str) -> types.TextContent:
    return types.TextContent(type="text", text=text)


def _result(text: str, data: dict[str, Any], images: list[types.ImageContent] | None = None) -> types.CallToolResult:
    return types.CallToolResult(content=[_text(text), *(images or [])], structured_content=data)


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[_text(text)], structured_content={"card": "error", "message": text}, is_error=True)


def parse_when(value: str, now_ms: int, tz_name: str) -> int:
    """Accept ISO 8601 ('2026-10-12T14:00') or a clock time today ('14:00', '2pm', 'tomorrow 9:30am')."""
    raw = value.strip().lower()
    zone = tz(tz_name)
    try:
        parsed = datetime.fromisoformat(value.strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return int(parsed.timestamp() * 1000)
    except ValueError:
        pass
    day = to_local(now_ms, tz_name).replace(hour=0, minute=0, second=0, microsecond=0)
    if raw.startswith("tomorrow"):
        day += timedelta(days=1)
        raw = raw.removeprefix("tomorrow").strip()
    raw = raw.removeprefix("today").strip()
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", raw)
    if not match:
        raise ValueError(f"Could not read a time from {value!r}. Use e.g. '14:00', '2pm' or '2026-10-12T14:00'.")
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    suffix = (match.group(3) or "").replace(".", "")
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"{value!r} is not a valid time of day.")
    return int((day + timedelta(hours=hour, minutes=minute)).timestamp() * 1000)


class PorchlightTools:
    """The tool implementations, kept separate from registration so tests can call them directly."""

    def __init__(self, policy: Policy, ingest: Ingest) -> None:
        self.policy = policy
        self.ingest = ingest
        self.settings = policy.settings

    # -- helpers -----------------------------------------------------------

    def _frame(self, obs: dict[str, Any] | None, max_width: int = 640) -> types.ImageContent | None:
        if not obs or not obs.get("snapshot_file"):
            return None
        path = self.settings.media_dir / obs["snapshot_file"]
        if not path.is_file():
            return None
        try:
            img = Image.open(path).convert("RGB")
            if img.width > max_width:
                img = img.resize((max_width, round(img.height * max_width / img.width)))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=72)
        except OSError:
            return None
        return types.ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode(), mime_type="image/jpeg")

    def _t(self, ms: int | None) -> str | None:
        return fmt_time(ms, self.settings.timezone) if ms else None

    def _check_in(self, exp: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": exp["id"],
            "title": exp["title"],
            "kind": exp["kind"],
            "state": exp["state"],
            "window": f"{self._t(exp['window_start'])} to {self._t(exp['window_end'])}",
            "window_start": exp["window_start"],
            "window_end": exp["window_end"],
            "arrived_at": self._t(exp["arrived_at"]),
            "completed_at": self._t(exp["completed_at"]),
            "unplanned": exp["unplanned"],
        }

    def _alert(self, alert: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": alert["id"],
            "level": alert["level"],
            "title": alert["title"],
            "body": alert["body"],
            "at": self._t(alert["ts"]),
            "state": alert["state"],
            "rule_id": alert["rule_id"],
            "rule": RULES.get(alert["rule_id"]),
            "check_in_id": alert["expectation_id"],
        }

    def _action(self, action: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": action["id"],
            "kind": action["kind"],
            "title": action["title"],
            "detail": action["detail"],
            "reason": action["reason"],
            "proposed_by": action["proposed_by"],
            "status": action["status"],
            "decided_by": action["decided_by"],
            "result": action["result"],
        }

    def _observation(self, obs: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": obs["id"],
            "at": self._t(obs["ts"]),
            "summary": obs["summary"],
            "package_present": obs["package_present"],
            "person_present": obs["person_present"],
            "confidence": round(obs["confidence"], 2),
            "status": obs["status"],
            "read_by": obs["reviewed_by"] or f"{obs['provider']}" + (f" ({obs['model']})" if obs["model"] else ""),
            "frame_source": obs["frame_source"],
            "frame_sha256": obs["snapshot_sha256"],
        }

    # -- tools ---------------------------------------------------------------

    async def porch_status(self) -> types.CallToolResult:
        status = self.policy.status()
        items = [self._check_in(e) for e in status["expectations"]]
        alerts = [self._alert(a) for a in status["alerts"]]
        actions = [self._action(a) for a in status["proposed_actions"]]
        last = status["last_observation"]
        lines = [status["headline"]]
        for alert in alerts:
            lines.append(f"- {alert['level'].upper()}: {alert['title']}. {alert['body']} (alert {alert['id']})")
        for item in items:
            lines.append(f"- {item['title']} ({item['kind']}, {item['window']}): {item['state'].replace('_', ' ')}")
        if actions:
            lines.append(f"{len(actions)} proposed action(s) are waiting for a family member to approve.")
        if last:
            lines.append(f"Latest frame at {self._t(last['ts'])}: {last['summary']}")
        frame = self._frame(last)
        data = {
            "card": "status",
            "person_name": status["person_name"],
            "tone": status["tone"],
            "headline": status["headline"],
            "check_ins": items,
            "alerts": alerts,
            "proposed_actions": actions,
            "needs_review": len(status["review_queue"]),
            "last_observation": self._observation(last) if last else None,
            "as_of": self._t(status["now"]),
        }
        return _result("\n".join(lines), data, [frame] if frame else None)

    async def list_check_ins(self, day: str = "today") -> types.CallToolResult:
        now = self.policy.clock.now_ms()
        start = self.policy._day_start(now) + (DAY_MS if day.strip().lower() == "tomorrow" else 0)
        self.policy.materialize_plans(now)
        rows = self.policy.store.query(
            "SELECT * FROM expectations WHERE state != 'cancelled' AND window_start >= ? AND window_start < ? "
            "ORDER BY window_start",
            [start, start + DAY_MS],
        )
        items = [self._check_in(r) for r in rows]
        label = "tomorrow" if start > now else "today"
        if not items:
            text = f"Nothing is expected {label}."
        else:
            text = f"{len(items)} check-in(s) {label}:\n" + "\n".join(
                f"- {i['title']} ({i['kind']}), {i['window']}: {i['state'].replace('_', ' ')} [{i['id']}]" for i in items
            )
        return _result(text, {"card": "check_ins", "day": label, "check_ins": items})

    async def get_evidence(self, subject_id: str) -> types.CallToolResult:
        chain = self.policy.evidence_for(subject_id)
        if not chain["subject"]:
            return _error(f"No check-in or alert with id {subject_id!r}.")
        steps = [
            {
                "at": self._t(e["ts"]),
                "what": e["what"],
                "by": e["actor"],
                "rule_id": e["rule_id"],
                "rule": e.get("rule"),
                "evidence": e["evidence"],
            }
            for e in chain["ledger"]
        ]
        observations = [self._observation(o) for o in chain["observations"]]
        events = [
            {"id": e["id"], "type": e["type"], "sub_type": e["sub_type"], "at": self._t(e["ts"]), "via": e["source"]}
            for e in chain["events"]
        ]
        lines = [f"Evidence for {chain['subject'].get('title', subject_id)}:"]
        for step in steps:
            rule = f" [rule {step['rule_id']}: {step['rule']}]" if step["rule_id"] else ""
            lines.append(f"- {step['at']} {step['what']} (by {step['by']}){rule}")
        for obs in observations:
            lines.append(
                f"- Frame {obs['id']} at {obs['at']}: {obs['summary']} "
                f"(confidence {obs['confidence']}, {obs['status']}, read by {obs['read_by']})"
            )
        for event in events:
            lines.append(f"- Ring event {event['type']} at {event['at']} via {event['via']}")
        frames = [f for f in (self._frame(o) for o in chain["observations"][-2:]) if f]
        data = {
            "card": "evidence",
            "title": chain["subject"].get("title"),
            "steps": steps,
            "observations": observations,
            "events": events,
        }
        return _result("\n".join(lines), data, frames)

    async def add_check_in(
        self,
        title: str,
        kind: str,
        start_time: str,
        end_time: str,
        collect_within_minutes: int | None = None,
    ) -> types.CallToolResult:
        now = self.policy.clock.now_ms()
        try:
            start = parse_when(start_time, now, self.settings.timezone)
            end = parse_when(end_time, now, self.settings.timezone)
            if end <= start < end + DAY_MS // 2:
                end += DAY_MS // 2  # "2pm" to "4": the second time inherits the afternoon
            exp = self.policy.add_expectation(
                title, kind.strip().lower(), start, end, collect_within_min=collect_within_minutes, actor="assistant"
            )
        except ValueError as exc:
            return _error(str(exc))
        if not exp:
            return _error("That check-in already exists.")
        self.ingest.changed()
        item = self._check_in(exp)
        day = to_local(start, self.settings.timezone).strftime("%A")
        return _result(
            f"Added: {item['title']} ({item['kind']}) on {day}, {item['window']}. Id {item['id']}.",
            {"card": "check_ins", "day": day, "check_ins": [item]},
        )

    async def cancel_check_in(self, check_in_id: str) -> types.CallToolResult:
        exp = self.policy.cancel_expectation(check_in_id, actor="assistant")
        if not exp:
            return _error(f"No check-in with id {check_in_id!r}.")
        self.ingest.changed()
        return _result(f"Cancelled: {exp['title']}.", {"card": "check_ins", "day": "", "check_ins": [self._check_in(exp)]})

    async def check_porch_now(self) -> types.CallToolResult:
        try:
            obs = await self.ingest.check_porch_now()
        except RingError as exc:
            return _error(f"Could not get a frame from Ring: {exc}")
        seen = self._observation(obs)
        if obs["status"] == "needs_review":
            text = (
                f"I fetched a frame at {seen['at']} but could not read it with confidence. "
                "It is in the review queue for a family member to look at."
            )
        else:
            text = f"At {seen['at']}: {obs['summary']}"
        frame = self._frame(obs)
        return _result(text, {"card": "frame", "observation": seen}, [frame] if frame else None)

    async def list_alerts(self, include_resolved: bool = False) -> types.CallToolResult:
        where = "level != 'info'" if include_resolved else "level != 'info' AND state = 'open'"
        rows = self.policy.store.query(f"SELECT * FROM alerts WHERE {where} ORDER BY ts DESC LIMIT 20")
        alerts = [self._alert(a) for a in rows]
        if not alerts:
            text = "No open alerts."
        else:
            text = "\n".join(f"- {a['level'].upper()} at {a['at']}: {a['title']}. {a['body']} [{a['id']}]" for a in alerts)
        return _result(text, {"card": "alerts", "alerts": alerts})

    async def acknowledge_alert(self, alert_id: str, acknowledged_by: str) -> types.CallToolResult:
        alert = self.policy.acknowledge_alert(alert_id, acknowledged_by.strip() or "family")
        if not alert:
            return _error(f"No alert with id {alert_id!r}.")
        self.ingest.changed()
        return _result(f"Acknowledged: {alert['title']}.", {"card": "alerts", "alerts": [self._alert(alert)]})

    async def propose_action(
        self, title: str, detail: str = "", kind: str = "check_in", check_in_id: str | None = None
    ) -> types.CallToolResult:
        action = self.policy.propose_action(
            kind=kind.strip().lower() or "check_in",
            title=title,
            detail=detail,
            reason="Requested through the assistant.",
            proposed_by="assistant",
            expectation_id=check_in_id,
        )
        if not action:
            return _error("Could not queue that request.")
        self.ingest.changed()
        return _result(
            f"Queued for approval: {action['title']}. Nothing has been done yet; "
            "a family member has to approve it on screen.",
            {"card": "actions", "proposed_actions": [self._action(action)]},
        )

    async def list_proposed_actions(self) -> types.CallToolResult:
        rows = self.policy.store.query("SELECT * FROM actions ORDER BY ts DESC LIMIT 10")
        actions = [self._action(a) for a in rows]
        waiting = [a for a in actions if a["status"] == "proposed"]
        text = (
            f"{len(waiting)} action(s) waiting for approval:\n" + "\n".join(f"- {a['title']} [{a['id']}]" for a in waiting)
            if waiting
            else "Nothing is waiting for approval."
        )
        return _result(text, {"card": "actions", "proposed_actions": actions})

    async def decide_action(self, action_id: str, approve: bool, decided_by: str = "family") -> types.CallToolResult:
        """App-only: reached from the Approve / Not now buttons on the card."""
        action = self.policy.decide_action(action_id, approve=approve, by=decided_by.strip() or "family")
        if not action:
            return _error(f"No action with id {action_id!r}.")
        self.ingest.changed()
        return _result(
            f"{action['title']}: {action['status']}. {action['result'] or ''}".strip(),
            {"card": "actions", "proposed_actions": [self._action(action)]},
        )


def build_mcp(policy: Policy, ingest: Ingest) -> tuple[MCPServer, PorchlightTools]:
    tools = PorchlightTools(policy, ingest)
    apps = Apps()
    apps.add_html_resource(
        CARD_URI,
        (WEB_DIR / "mcp-card.html").read_text(encoding="utf-8"),
        name="Porchlight card",
        description="Status, evidence and approval cards for Porchlight tool results.",
        prefers_border=False,
    )

    @apps.tool(
        resource_uri=CARD_URI,
        name="porch_status",
        title="Porch status",
        description=(
            "How things stand at the front door right now: today's expected deliveries and visits, "
            "open alerts, requests waiting for approval, and the latest frame. Start here."
        ),
        annotations=READ_ONLY,
    )
    async def porch_status() -> types.CallToolResult:
        return await tools.porch_status()

    @apps.tool(
        resource_uri=CARD_URI,
        name="get_evidence",
        title="Show the evidence",
        description=(
            "The evidence behind a check-in or alert: each state change, the rule that made it, "
            "and the camera frames it rests on. Pass a check-in id (exp_...) or alert id (alt_...)."
        ),
        annotations=READ_ONLY,
    )
    async def get_evidence(subject_id: str) -> types.CallToolResult:
        return await tools.get_evidence(subject_id)

    @apps.tool(
        resource_uri=CARD_URI,
        name="check_porch_now",
        title="Look at the porch now",
        description="Fetch the most recent frame from the Ring doorbell and report what it shows.",
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    )
    async def check_porch_now() -> types.CallToolResult:
        return await tools.check_porch_now()

    @apps.tool(
        resource_uri=CARD_URI,
        name="list_proposed_actions",
        title="Requests waiting for approval",
        description="Actions that Porchlight or the assistant proposed, and whether a family member approved them.",
        annotations=READ_ONLY,
    )
    async def list_proposed_actions() -> types.CallToolResult:
        return await tools.list_proposed_actions()

    @apps.tool(
        resource_uri=CARD_URI,
        visibility=["app"],
        name="decide_action",
        title="Approve or decline",
        description="Approve or decline a proposed action. Only callable from the on-screen card.",
        annotations=WRITES,
    )
    async def decide_action(action_id: str, approve: bool, decided_by: str = "family") -> types.CallToolResult:
        return await tools.decide_action(action_id, approve, decided_by)

    server = MCPServer(
        "porchlight",
        title="Porchlight",
        description="Care check-ins from the front door, backed by Ring.",
        instructions=INSTRUCTIONS,
        version=__version__,
        extensions=[apps],
        log_level="WARNING",
    )

    @server.tool(
        name="list_check_ins",
        title="List check-ins",
        description="Expected deliveries and visits for 'today' or 'tomorrow', with their current state.",
        annotations=READ_ONLY,
    )
    async def list_check_ins(day: str = "today") -> types.CallToolResult:
        return await tools.list_check_ins(day)

    @server.tool(
        name="add_check_in",
        title="Add a check-in",
        description=(
            "Tell Porchlight to expect a delivery or a visit. kind is 'delivery' or 'visit'. "
            "Times are local: '14:00', '2pm', 'tomorrow 9am' or ISO 8601. For deliveries, "
            "collect_within_minutes is how long the package may sit before the family is told."
        ),
        annotations=WRITES,
    )
    async def add_check_in(
        title: str,
        kind: str,
        start_time: str,
        end_time: str,
        collect_within_minutes: int | None = None,
    ) -> types.CallToolResult:
        return await tools.add_check_in(title, kind, start_time, end_time, collect_within_minutes)

    @server.tool(
        name="cancel_check_in",
        title="Cancel a check-in",
        description="Stop expecting a delivery or visit. Pass the check-in id.",
        annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    )
    async def cancel_check_in(check_in_id: str) -> types.CallToolResult:
        return await tools.cancel_check_in(check_in_id)

    @server.tool(
        name="list_alerts",
        title="List alerts",
        description="Open alerts that need a family member's attention. Set include_resolved for history.",
        annotations=READ_ONLY,
    )
    async def list_alerts(include_resolved: bool = False) -> types.CallToolResult:
        return await tools.list_alerts(include_resolved)

    @server.tool(
        name="acknowledge_alert",
        title="Acknowledge an alert",
        description="Mark an alert as seen by a named family member. This does not resolve the underlying issue.",
        annotations=WRITES,
    )
    async def acknowledge_alert(alert_id: str, acknowledged_by: str) -> types.CallToolResult:
        return await tools.acknowledge_alert(alert_id, acknowledged_by)

    @server.tool(
        name="propose_action",
        title="Propose an action",
        description=(
            "Queue a request for the family, such as asking a neighbour to knock (kind 'check_in') or "
            "rescheduling a delivery (kind 'reschedule', with check_in_id). This never carries the action out: "
            "a family member must approve it on screen."
        ),
        annotations=WRITES,
    )
    async def propose_action(
        title: str, detail: str = "", kind: str = "check_in", check_in_id: str | None = None
    ) -> types.CallToolResult:
        return await tools.propose_action(title, detail, kind, check_in_id)

    return server, tools
