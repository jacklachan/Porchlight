"""A simulated Alexa+ host for Porchlight's MCP server.

Hackathon participants cannot register a real Alexa+ add-on, so this plays the
host's part: it connects to the MCP server over Streamable HTTP exactly as an
outside agent would, lists its tools, picks which to call for an utterance,
and hands the tool results (and their MCP Apps cards) to the web page.

Two ways to pick tools:

* "bedrock": a Bedrock model chooses tools through the Converse API.
* "rules": a small keyword router, so the surface works with no model at all.

Either way the model-facing tool list excludes tools whose MCP Apps visibility
is app-only, so approving an action is something only a person's tap can do.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import Client, types
from mcp.client import advertise
from mcp.client.streamable_http import streamable_http_client
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.shared._httpx_utils import create_mcp_http_client

from .config import Settings

SYSTEM = (
    "You are a voice assistant on a smart display, helping a family member check on a relative who "
    "lives alone, using the Porchlight tools. Answer in one to three short spoken sentences, plain "
    "words, no lists or markdown. Say times the way people say them. If something needs attention, "
    "lead with it. Never claim an action was carried out: propose_action only queues a request that "
    "the person must approve on screen. Do not describe people in frames beyond 'a person'."
)

HELP = (
    "You can ask me how things are at the door, whether a delivery arrived, to look at the porch now, "
    "why an alert was raised, or to expect a delivery or a visit."
)


def tool_is_model_visible(tool: types.Tool) -> bool:
    ui = (tool.meta or {}).get("ui") or {}
    visibility = ui.get("visibility")
    return visibility is None or "model" in visibility


def tool_card_uri(tool: types.Tool | None) -> str | None:
    if tool is None:
        return None
    return ((tool.meta or {}).get("ui") or {}).get("resourceUri")


def dump_result(result: types.CallToolResult) -> dict[str, Any]:
    return result.model_dump(by_alias=True, exclude_none=True, mode="json")


def result_text(result: types.CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, types.TextContent))


class Assistant:
    def __init__(self, settings: Settings, mcp_url: str) -> None:
        self.settings = settings
        self.mcp_url = mcp_url
        self._bedrock: Any | None = None

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Client]:
        headers = {"Authorization": f"Bearer {self.settings.mcp_auth_token}"} if self.settings.mcp_auth_token else None
        async with create_mcp_http_client(headers=headers) as http:
            transport = streamable_http_client(self.mcp_url, http_client=http)
            ui = advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})
            async with Client(transport, extensions=[ui], cache=None) as client:
                yield client

    # -- used by the page's card host ---------------------------------------

    async def read_card(self, uri: str) -> str:
        async with self.connect() as client:
            result = await client.read_resource(uri)
        for item in result.contents:
            text = getattr(item, "text", None)
            if text:
                return text
        raise ValueError(f"{uri} has no text content")

    async def call_from_card(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """A tool call that started with a tap inside a card. App-only tools are allowed here."""
        result: types.CallToolResult | None = None
        async with self.connect() as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            if tool_card_uri(tools.get(name)):
                result = await client.call_tool(name, arguments)
        # Raised out here: an exception inside the client's task group arrives wrapped in a group.
        if result is None:
            raise ValueError(f"{name} is not a card tool")
        return dump_result(result)

    # -- one utterance -------------------------------------------------------

    async def handle(self, text: str, speaker: str = "family") -> dict[str, Any]:
        text = text.strip()
        async with self.connect() as client:
            listing = (await client.list_tools()).tools
            by_name = {t.name: t for t in listing}
            visible = [t for t in listing if tool_is_model_visible(t)]
            calls: list[dict[str, Any]] = []

            async def call(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
                if name not in {t.name for t in visible}:
                    return types.CallToolResult(
                        content=[types.TextContent(type="text", text=f"{name} is not available to the assistant.")],
                        is_error=True,
                    )
                result = await client.call_tool(name, arguments)
                calls.append(
                    {
                        "name": name,
                        "arguments": arguments,
                        "result": dump_result(result),
                        "card_uri": tool_card_uri(by_name.get(name)),
                    }
                )
                return result

            router = "rules"
            speech: str | None = None
            if self.settings.assistant_provider == "bedrock":
                try:
                    speech = await self._bedrock_turn(text, visible, call)
                    router = f"bedrock:{self.settings.assistant_model_id}"
                except Exception as exc:  # fall back rather than leave the person with silence
                    calls.clear()
                    router = f"rules (bedrock failed: {type(exc).__name__})"
            if speech is None:
                speech = await self._rules_turn(text, speaker, call)
            return {
                "speech": speech,
                "router": router,
                "protocol_version": client.protocol_version,
                "server": client.server_info.name if client.server_info else None,
                "tool_calls": calls,
            }

    # -- rules router --------------------------------------------------------

    async def _rules_turn(self, text: str, speaker: str, call: Any) -> str:
        t = text.lower()

        def has(*words: str) -> bool:
            return any(re.search(rf"\b{w}", t) for w in words)

        if has("why", "evidence", "proof", "how do you know", "show me"):
            status = (await call("porch_status", {})).structured_content or {}
            subject = None
            if status.get("alerts"):
                subject = status["alerts"][0]["id"]
            else:
                started = [c for c in status.get("check_ins", []) if c["state"] != "scheduled"]
                if started:
                    subject = started[-1]["id"]
            if not subject:
                return "There is nothing to show evidence for yet. Nothing has happened at the door today."
            data = (await call("get_evidence", {"subject_id": subject})).structured_content or {}
            steps = [s for s in data.get("steps", []) if s.get("rule_id")]
            if not steps:
                return "Here is what I have on that."
            last = steps[-1]
            frames = len(data.get("observations", []))
            return (
                f"Here is the evidence for {data.get('title')}. The last change was at {last['at']}: "
                f"{last['what']}, because {last['rule'][0].lower() + last['rule'][1:]} "
                f"It rests on {frames} camera frame{'s' if frames != 1 else ''}, shown on screen."
            )

        if has("look", "check the porch", "check now", "right now", "still there", "still on"):
            data = (await call("check_porch_now", {})).structured_content or {}
            if data.get("card") == "error":
                return data["message"]
            obs = data["observation"]
            if obs["status"] == "needs_review":
                return "I got a frame but I am not confident about what it shows, so I have put it up for you to check."
            return f"I just looked. {obs['summary']}"

        if has("expect", "add", "schedule", "coming", "remind") and has("deliver", "package", "parcel", "visit", "aide", "nurse", "carer", "meds", "medic", "pharmacy", "meal"):
            parsed = _parse_check_in(text)
            if not parsed:
                return "Tell me what to expect and when. For example: expect a pharmacy delivery between 2 and 4 pm."
            data = (await call("add_check_in", parsed)).structured_content or {}
            if data.get("card") == "error":
                return data["message"]
            item = data["check_ins"][0]
            return f"Okay. I will watch for {item['title']} between {item['window']}, and tell you if it does not go to plan."

        if has("ask", "call", "neighbo", "knock", "check on", "someone to"):
            status = (await call("porch_status", {})).structured_content or {}
            alerts = status.get("alerts") or []
            args: dict[str, Any] = {"title": "Ask a neighbour to knock on the door", "kind": "check_in"}
            if alerts:
                args["detail"] = alerts[0]["body"]
                args["check_in_id"] = alerts[0].get("check_in_id")
            await call("propose_action", args)
            await call("list_proposed_actions", {})
            return "I have queued that request. Nothing is sent until you approve it on screen."

        if has("acknowledge", "got it", "i know", "i've seen", "dismiss"):
            data = (await call("list_alerts", {})).structured_content or {}
            alerts = data.get("alerts") or []
            if not alerts:
                return "There are no open alerts to acknowledge."
            await call("acknowledge_alert", {"alert_id": alerts[0]["id"], "acknowledged_by": speaker})
            return f"Marked as seen: {alerts[0]['title']}. I will keep watching until it is resolved."

        if has("tomorrow"):
            data = (await call("list_check_ins", {"day": "tomorrow"})).structured_content or {}
            items = data.get("check_ins") or []
            if not items:
                return "Nothing is expected tomorrow."
            return "Tomorrow: " + "; ".join(f"{i['title']} between {i['window']}" for i in items) + "."

        if has("approv", "waiting", "request", "pending"):
            data = (await call("list_proposed_actions", {})).structured_content or {}
            waiting = [a for a in data.get("proposed_actions", []) if a["status"] == "proposed"]
            if not waiting:
                return "Nothing is waiting for your approval."
            return f"{waiting[0]['title']} is waiting for your approval on screen."

        if has("how", "status", "okay", "ok", "alright", "arrive", "deliver", "package", "parcel", "meds", "medic",
               "door", "porch", "today", "anything", "alert", "wrong", "visit", "aide", "come"):
            data = (await call("porch_status", {})).structured_content or {}
            return _speak_status(data)

        return HELP

    # -- bedrock -------------------------------------------------------------

    async def _bedrock_turn(self, text: str, tools: list[types.Tool], call: Any) -> str:
        if self._bedrock is None:
            import boto3  # optional dependency: pip install "porchlight[aws]"

            self._bedrock = boto3.client("bedrock-runtime", region_name=self.settings.aws_region)
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": t.name,
                        "description": (t.description or t.name)[:1000],
                        "inputSchema": {"json": t.input_schema},
                    }
                }
                for t in tools
            ]
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": text}]}]
        for _ in range(5):
            response = await asyncio.to_thread(
                self._bedrock.converse,
                modelId=self.settings.assistant_model_id,
                system=[{"text": SYSTEM}],
                messages=messages,
                toolConfig=tool_config,
                inferenceConfig={"maxTokens": 500, "temperature": 0.2},
            )
            message = response["output"]["message"]
            messages.append(message)
            uses = [b["toolUse"] for b in message["content"] if "toolUse" in b]
            if response.get("stopReason") != "tool_use" or not uses:
                spoken = " ".join(b["text"] for b in message["content"] if "text" in b).strip()
                return re.sub(r"<thinking>.*?</thinking>", "", spoken, flags=re.S).strip() or "Done."
            results = []
            for use in uses:
                result = await call(use["name"], use.get("input") or {})
                results.append(
                    {
                        "toolResult": {
                            "toolUseId": use["toolUseId"],
                            "content": [{"text": result_text(result)[:6000] or "(no text)"}],
                            "status": "error" if result.is_error else "success",
                        }
                    }
                )
            messages.append({"role": "user", "content": results})
        return "I could not finish that. Please try asking in a simpler way."


def _speak_status(data: dict[str, Any]) -> str:
    alerts = data.get("alerts") or []
    items = data.get("check_ins") or []
    parts: list[str] = []
    if alerts:
        first = alerts[0]
        parts.append(f"One thing needs your attention. {first['title']}. {first['body']}")
        if len(alerts) > 1:
            parts.append(f"There {'is' if len(alerts) == 2 else 'are'} {len(alerts) - 1} more on screen.")
    else:
        parts.append(data.get("headline", "All quiet."))
        done = [i for i in items if i["state"] == "completed"]
        upcoming = [i for i in items if i["state"] == "scheduled"]
        if done:
            last = done[-1]
            verb = "was brought in" if last["kind"] == "delivery" else "happened"
            parts.append(f"{last['title']} {verb} at {last['completed_at']}.")
        if upcoming:
            parts.append(f"Next is {upcoming[0]['title']}, expected between {upcoming[0]['window']}.")
    if data.get("proposed_actions"):
        parts.append("There is a request waiting for your approval on screen.")
    if data.get("needs_review"):
        parts.append("I also have a frame I could not read. Could you take a look?")
    return " ".join(parts)


_TIME = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"


def _parse_check_in(text: str) -> dict[str, Any] | None:
    """'expect a pharmacy delivery tomorrow between 2 and 4 pm' -> add_check_in arguments."""
    t = text.lower()
    kind = "visit" if re.search(r"\b(visit|aide|nurse|carer|caregiver|doctor|physio)", t) else "delivery"
    day = "tomorrow " if "tomorrow" in t else ""

    between = re.search(rf"(?:between|from)\s+{_TIME}\s*(?:and|to|-)\s*{_TIME}", t)
    at = re.search(rf"\b(?:at|around|by)\s+{_TIME}", t)
    if between:
        start, end = between.group(1).strip(), between.group(2).strip()
        suffix = re.search(r"(am|pm)$", end)
        if suffix and not re.search(r"(am|pm)$", start):
            start_hour, end_hour = int(re.match(r"\d+", start).group()), int(re.match(r"\d+", end).group())  # type: ignore[union-attr]
            # "between 2 and 4 pm": both are pm unless that would run backwards ("11 and 1 pm")
            start += " am" if suffix.group(1) == "pm" and start_hour > end_hour and start_hour != 12 else f" {suffix.group(1)}"
    elif at:
        start = at.group(1).strip()
        hour_match = re.match(r"(\d+)(?::(\d{2}))?\s*(am|pm)?", start)
        assert hour_match
        hour = int(hour_match.group(1)) + 1
        suffix_text = hour_match.group(3) or ""
        if hour == 12 and suffix_text:
            suffix_text = "pm" if suffix_text == "am" else "am"
        if hour > 12 and suffix_text:
            hour -= 12
        end = f"{hour}:{hour_match.group(2) or '00'} {suffix_text}".strip()
    else:
        return None

    title = re.sub(rf"(?:between|from)\s+{_TIME}\s*(?:and|to|-)\s*{_TIME}|\b(?:at|around|by)\s+{_TIME}", "", t)
    title = re.sub(
        r"\b(alexa|please|can you|could you|expect|add|schedule|remind me|there is|there's|is coming|coming|"
        r"tomorrow|today|a|an|the|for|mom|mum|her|his|my|to|that|will be|arriving)\b",
        " ",
        title,
    )
    title = re.sub(r"[^a-z' ]", " ", title)
    title = re.sub(r"\s+", " ", title).strip().title() or kind.title()
    return {"title": title, "kind": kind, "start_time": day + start, "end_time": day + end}
