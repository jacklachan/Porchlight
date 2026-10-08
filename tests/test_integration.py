"""End to end over real HTTP: the app, the local Ring stand-in, the webhook path and the MCP server.

A uvicorn server runs in a thread so the Ring client, the simulated webhook
sender and the MCP client all make genuine network calls.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import Client

from porchlight.api import create_app
from porchlight.assistant import tool_is_model_visible
from porchlight.config import Settings
from porchlight.ring import sign


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    settings = Settings(
        data_dir=tmp_path_factory.mktemp("data"),
        demo=True,
        port=port,
        ring_api_base=f"http://127.0.0.1:{port}/sim",
        ring_access_token="sim-token",
        ring_hmac_signing_key="test-signing-key",
        vision_provider="fixture",
        poll_seconds=3600,  # the tests drive polling themselves
        timezone="Asia/Kolkata",
    )
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        raise RuntimeError("server did not start")
    yield base, settings
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def http(server):
    base, _ = server
    with httpx.Client(base_url=base, timeout=20) as client:
        client.post("/api/demo/reset")
        yield client


def status(http):
    return http.get("/api/status").json()


def wait_for(http, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = status(http)
        if predicate(data):
            return data
        time.sleep(0.1)
    raise AssertionError(f"condition not met; last status headline: {data['headline']}")


def expect_delivery(http, minutes=120):
    r = http.post(
        "/api/expectations",
        json={
            "title": "Pharmacy delivery",
            "kind": "delivery",
            "start_time": "00:00",
            "end_time": "23:59",
            "collect_within_minutes": minutes,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["expectation"]


def test_delivery_lifecycle_through_webhook_snapshot_and_rules(http):
    exp = expect_delivery(http)

    fired = http.post("/api/demo/scene", json={"scene": "delivery", "event": "motion"}).json()
    assert fired["webhook"] == 200, "the stand-in's signed webhook should be accepted"

    data = wait_for(http, lambda d: d["expectations"][0]["state"] == "arrived")
    obs = data["last_observation"]
    assert obs["status"] == "trusted" and obs["package_present"] is True
    assert obs["frame_source"] == "ring_snapshot" and len(obs["snapshot_sha256"]) == 64
    assert http.get(obs["frame_url"]).headers["content-type"] == "image/jpeg"

    # one moment, one event: the webhook and the history record are not double counted
    events = http.get("/api/activity").json()["events"]
    assert len(events) == 1 and events[0]["source"] == "webhook"

    http.post("/api/demo/advance", json={"minutes": 125})
    data = status(http)
    assert data["tone"] == "attention"
    assert data["alerts"][0]["rule_id"] == "delivery.uncollected"

    http.post("/api/demo/advance", json={"minutes": 125})
    data = status(http)
    assert data["tone"] == "urgent"
    assert [a["rule_id"] for a in data["alerts"]] == ["delivery.uncollected.escalate"]
    assert data["proposed_actions"][0]["kind"] == "check_in"

    chain = http.get(f"/api/evidence/{data['alerts'][0]['id']}").json()
    assert [e["rule_id"] for e in chain["ledger"] if e["rule_id"]] == [
        "delivery.arrived",
        "delivery.uncollected",
        "delivery.uncollected.escalate",
    ]
    assert chain["observations"] and chain["events"]

    http.post("/api/demo/scene", json={"scene": "pickup", "event": "motion"})
    data = wait_for(http, lambda d: d["expectations"][0]["state"] == "completed")
    assert data["tone"] == "good" and data["alerts"] == [] and data["proposed_actions"] == []
    assert exp["id"] == data["expectations"][0]["id"]


def test_webhook_signature_is_enforced_and_deliveries_are_idempotent(http, server):
    _, settings = server
    payload = {
        "meta": {"version": "1.1", "time": "t", "request_id": "req-1", "account_id": "acct"},
        "data": {
            "id": "dev_button_press_1",
            "type": "button_press",
            "attributes": {"source": "dev", "source_type": "devices", "timestamp": status(http)["now"]},
        },
    }
    raw = json.dumps(payload).encode()
    headers = {"content-type": "application/json"}

    assert http.post("/webhooks/ring", content=raw, headers=headers).status_code == 401
    bad = {**headers, "X-Signature": sign("wrong-key", raw)}
    assert http.post("/webhooks/ring", content=raw, headers=bad).status_code == 401

    good = {**headers, "X-Signature": sign(settings.ring_hmac_signing_key, raw)}
    assert http.post("/webhooks/ring", content=raw, headers=good).json()["status"] == "stored"
    assert http.post("/webhooks/ring", content=raw, headers=good).json()["status"] == "duplicate"

    # same bytes, different whitespace: the signature must be over the raw body
    reserialised = json.dumps(payload, indent=2).encode()
    assert http.post("/webhooks/ring", content=reserialised, headers=good).status_code == 401


def test_doorbell_press_confirms_visit(http):
    http.post(
        "/api/expectations",
        json={"title": "Home aide", "kind": "visit", "start_time": "00:00", "end_time": "23:59"},
    )
    http.post("/api/demo/scene", json={"scene": "visitor", "event": "ding"})
    data = wait_for(http, lambda d: d["expectations"][0]["state"] == "completed")
    assert data["expectations"][0]["completion_event"]


def test_browser_frame_goes_through_the_same_gate(http):
    expect_delivery(http)
    frame = http.get("/sim/_control/frame").content  # the porch is empty after reset
    r = http.post("/api/observations/frame", content=frame, headers={"content-type": "image/jpeg"})
    obs = r.json()["observation"]
    assert obs["frame_source"] == "ring_live_view" and obs["package_present"] is False

    # a frame the provider cannot read is held for a person, and their answer is what counts
    r = http.post("/api/observations/frame", content=_plain_jpeg(), headers={"content-type": "image/jpeg"})
    held = r.json()["observation"]
    assert held["status"] == "needs_review"
    assert status(http)["expectations"][0]["state"] == "scheduled"
    http.post(f"/api/observations/{held['id']}/review", json={"package_present": True, "reviewer": "Asha"})
    assert status(http)["expectations"][0]["state"] == "arrived"


def _plain_jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (90, 90, 90)).save(buf, format="JPEG")
    return buf.getvalue()


async def test_mcp_server_over_streamable_http(http, server):
    base, _ = server
    expect_delivery(http)
    http.post("/api/demo/scene", json={"scene": "package", "event": "motion"})
    wait_for(http, lambda d: d["expectations"][0]["state"] == "arrived")

    async with Client(f"{base}/mcp") as client:
        assert client.protocol_version >= "2025-11-25"
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert {"porch_status", "get_evidence", "add_check_in", "propose_action", "decide_action"} <= set(tools)
        assert not tool_is_model_visible(tools["decide_action"]), "approval must not be offered to the model"
        assert tools["porch_status"].meta["ui"]["resourceUri"] == "ui://porchlight/card.html"

        result = await client.call_tool("porch_status", {})
        assert result.structured_content["check_ins"][0]["state"] == "arrived"
        assert [block.type for block in result.content] == ["text", "image"]

        exp_id = result.structured_content["check_ins"][0]["id"]
        evidence = await client.call_tool("get_evidence", {"subject_id": exp_id})
        assert evidence.structured_content["steps"][-1]["rule_id"] == "delivery.arrived"

        added = await client.call_tool(
            "add_check_in",
            {"title": "Meal drop", "kind": "delivery", "start_time": "tomorrow 1pm", "end_time": "tomorrow 2pm"},
        )
        assert not added.is_error

        proposed = await client.call_tool("propose_action", {"title": "Ask Mrs Rao to knock"})
        action = proposed.structured_content["proposed_actions"][0]
        assert action["status"] == "proposed"

        card = await client.read_resource("ui://porchlight/card.html")
        assert card.contents[0].mime_type == "text/html;profile=mcp-app"

    assert status(http)["proposed_actions"][0]["id"] == action["id"]


def test_assistant_answers_and_only_a_tap_can_approve(http):
    expect_delivery(http, minutes=60)
    http.post("/api/demo/scene", json={"scene": "package", "event": "motion"})
    wait_for(http, lambda d: d["expectations"][0]["state"] == "arrived")
    http.post("/api/demo/advance", json={"minutes": 130})

    turn = http.post("/api/assistant", json={"text": "How is Mom doing?"}).json()
    assert "needs your attention" in turn["speech"]
    assert turn["tool_calls"][0]["name"] == "porch_status"
    assert turn["tool_calls"][0]["card_uri"] == "ui://porchlight/card.html"

    turn = http.post("/api/assistant", json={"text": "Why? Show me the evidence."}).json()
    assert [c["name"] for c in turn["tool_calls"]] == ["porch_status", "get_evidence"]

    action = status(http)["proposed_actions"][0]
    # the assistant has no way to approve: it is not in its tool list
    turn = http.post("/api/assistant", json={"text": "What is waiting for approval?"}).json()
    assert all(c["name"] != "decide_action" for c in turn["tool_calls"])

    # a tap inside the card can
    r = http.post(
        "/api/assistant/card-call",
        json={"name": "decide_action", "arguments": {"action_id": action["id"], "approve": True, "decided_by": "Asha"}},
    )
    assert r.status_code == 200
    decided = r.json()["structuredContent"]["proposed_actions"][0]
    assert decided["status"] == "approved" and decided["decided_by"] == "Asha"
    # and card-call refuses tools that have no card
    r = http.post("/api/assistant/card-call", json={"name": "cancel_check_in", "arguments": {"check_in_id": "x"}})
    assert r.status_code == 400


def test_expired_ring_token_is_reported_not_swallowed(http):
    r = http.post("/api/ring/token", json={"token": "expired-playground-token"})
    assert r.status_code == 401 and "Ring API 401" in r.json()["detail"]
    r = http.post("/api/porch/check")
    assert r.status_code == 401

    r = http.post("/api/ring/token", json={"token": "sim-token"})
    assert r.json()["connection"]["device_name"] == "Front Door (simulated)"
    assert http.post("/api/porch/check").status_code == 200
