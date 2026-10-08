"""Small pieces: webhook parsing, time parsing, the assistant's phrase parser, Bedrock output parsing."""

import json

import pytest
from conftest import TZ, at

from porchlight.assistant import _parse_check_in
from porchlight.mcp_server import parse_when
from porchlight.ring import WebhookError, parse, sign, verify_signature
from porchlight.ring.simulator import read_labels, render_scene
from porchlight.vision import frames
from porchlight.vision.bedrock import parse_tool_output
from porchlight.vision.openai_compat import OpenAICompatVision, parse_json_reply

MOTION = {
    "meta": {"version": "1.1", "time": "2026-02-13T13:39:57Z", "request_id": "r-1", "account_id": "ava1.ring.account.X"},
    "data": {
        "id": "dev_motion_1786715596787",
        "type": "motion_detected",
        "attributes": {
            "source": "dev",
            "source_type": "devices",
            "timestamp": 1786715596787,
            "sub_type": "human",
            "component_ids": ["0"],
        },
    },
}


def test_webhook_parse_reads_the_documented_motion_payload():
    event = parse(json.dumps(MOTION).encode())
    assert (event.type, event.sub_type, event.device_id) == ("motion_detected", "human", "dev")
    assert event.timestamp_ms == 1786715596787 and event.component_ids == ["0"]
    assert event.is_device_event


def test_subscription_webhook_is_not_a_device_event():
    payload = {
        "meta": {"version": "1.1", "time": "t", "request_id": "r-2", "account_id": "a"},
        "data": {"id": "e", "type": "subscription_activated", "attributes": {"source": "subscriptions"}},
    }
    event = parse(json.dumps(payload).encode())
    assert not event.is_device_event and event.device_id is None


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"meta": {}, "data": {}}'])
def test_webhook_parse_rejects_other_shapes(body):
    with pytest.raises(WebhookError):
        parse(body)


def test_signature_uses_hex_digest_with_sha256_prefix():
    raw = b'{"a":1}'
    header = sign("key", raw)
    assert header.startswith("sha256=") and len(header) == 7 + 64
    assert verify_signature("key", raw, header)
    assert verify_signature("key", raw, header.removeprefix("sha256="))
    assert not verify_signature("key", raw + b" ", header)
    assert not verify_signature("key", raw, None)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("14:00", at(14)),
        ("2pm", at(14)),
        ("2:30 PM", at(14, 30)),
        ("12am", at(0)),
        ("tomorrow 9am", at(9, day=13)),
        ("2026-10-12T14:00", at(14)),
    ],
)
def test_parse_when(text, expected):
    assert parse_when(text, at(8), TZ) == expected


def test_parse_when_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_when("sometime after lunch", at(8), TZ)


def test_assistant_reads_a_spoken_delivery_window():
    parsed = _parse_check_in("Expect a pharmacy delivery tomorrow between 2 and 4 pm")
    assert parsed == {
        "title": "Pharmacy Delivery",
        "kind": "delivery",
        "start_time": "tomorrow 2 pm",
        "end_time": "tomorrow 4 pm",
    }
    parsed = _parse_check_in("the nurse is coming at 9 am")
    assert parsed["kind"] == "visit" and parsed["start_time"] == "9 am" and parsed["end_time"] == "10:00 am"
    assert _parse_check_in("expect a delivery") is None


def test_stand_in_frames_carry_their_labels_and_other_images_do_not():
    labels = read_labels(render_scene("delivery"))
    assert labels["package"] and labels["person"] and labels["vehicle"]
    assert read_labels(b"not an image") is None


def test_bedrock_output_parsing_clamps_and_survives_bad_output():
    good = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "report_porch",
                            "input": {
                                "package_present": True,
                                "person_present": False,
                                "vehicle_present": False,
                                "confidence": 1.4,
                                "summary": "A box on the mat.",
                            },
                        }
                    }
                ]
            }
        }
    }
    reading = parse_tool_output(good, "bedrock", "m")
    assert reading.package_present is True and reading.confidence == 1.0 and reading.error is None

    prose = {"output": {"message": {"content": [{"text": "I think there is a box."}]}}}
    reading = parse_tool_output(prose, "bedrock", "m")
    assert reading.package_present is None and reading.error


def test_frame_fingerprint_ignores_recompression_but_sees_a_parcel():
    import io

    from PIL import Image

    empty, package = render_scene("empty"), render_scene("package")
    buf = io.BytesIO()
    Image.open(io.BytesIO(package)).resize((480, 270)).save(buf, format="JPEG", quality=40)
    assert frames.unchanged(frames.fingerprint(package), frames.fingerprint(buf.getvalue()))
    assert not frames.unchanged(frames.fingerprint(package), frames.fingerprint(empty))
    assert not frames.unchanged(frames.fingerprint(package), None)
    assert frames.quality(package) is None and frames.quality(b"junk") == "not a readable image"


def test_openai_compatible_reply_parsing():
    fenced = """```json
{"package_present": true, "person_present": false, "vehicle_present": false, "confidence": 0.9, "summary": "A box."}
```"""
    reading = parse_json_reply(fenced, "openai", "m")
    assert reading.package_present is True and reading.confidence == 0.9
    assert parse_json_reply("I cannot tell.", "openai", "m").error
    assert parse_json_reply('{"package_present": "yes", "person_present": false, "vehicle_present": false, "confidence": 1}', "openai", "m").error


async def test_openai_compatible_provider_sends_the_image_and_survives_errors():
    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        seen["image"] = body["messages"][0]["content"][1]["image_url"]["url"][:23]
        reply = {"package_present": False, "person_present": True, "vehicle_present": False, "confidence": 0.8, "summary": "A person."}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    vision = OpenAICompatVision("http://llm.test/v1", "some-model", "key", transport=httpx.MockTransport(handler))
    reading = await vision.read(b"\xff\xd8fake", "image/jpeg")
    assert reading.person_present is True and reading.model == "some-model"
    assert seen == {"auth": "Bearer key", "image": "data:image/jpeg;base64,"}

    down = OpenAICompatVision("http://llm.test/v1", "m", transport=httpx.MockTransport(lambda r: httpx.Response(429, text="slow down")))
    assert "429" in (await down.read(b"x", "image/jpeg")).error
