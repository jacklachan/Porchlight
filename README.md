# Porchlight

**Care check-ins from the front door, built on the Ring Partner API.**

A parent who lives alone has a pharmacy delivery on Tuesday, a meal drop at 1 pm and a home aide at 9 am.
Porchlight watches their Ring doorbell for those things and tells the family when one does not go to plan:

> *Pharmacy delivery arrived at 2:14 PM and has not been brought in 2 hours 10 min later. Mom usually collects within 2 hours.*

A package nobody picked up is not a security event. It is often the first outside sign that someone is unwell.
Porchlight turns it into a calm, specific message, with the camera frame it rests on.

Built for the **Build, Ship, Shape: Amazon Developer Hackathon**. Primary track: **Ring** (caretaking,
package management, a non-security use). Also entered in **Alexa+** through a self-hosted MCP server.

---

## What it does

| | |
|---|---|
| **Expect** | The family says what should happen: a delivery or a visit, a time window, and how long a package may sit. One-off or repeating. |
| **Verify** | Ring events arrive by signed webhook or Event History polling. For each one Porchlight fetches the frame from Ring's image API and reads it. |
| **Decide** | Deterministic rules, not the model, move each check-in: *arrived*, *brought in*, *still outside*, *not seen*. |
| **Tell** | Alerts say what happened, when, and why, and link to the frames. Anything Porchlight wants to *do* waits for a person to approve. |
| **Ask** | An MCP server lets an assistant answer "Did Mom's meds arrive?" with the frame as evidence. |

### The rule that shapes everything: a model may advise, it may not act

A vision model describes a frame and says how sure it is. That reading only changes a check-in when:

- its confidence is at or above the threshold (default 75%), **or**
- a family member looked at the frame and confirmed it.

Otherwise the frame goes to a "your eyes needed" queue and nothing changes. With `VISION_PROVIDER=none`
Porchlight still works: every frame is confirmed by a person.

Every state change is written to an append-only ledger with the rule that made it and the observation
and Ring event ids behind it. "See the evidence" on any alert shows that chain.

Porchlight never identifies anyone. The model is told to report only *package / person / vehicle present*.

---

## How Ring is used

All calls are in [`porchlight/ring/client.py`](porchlight/ring/client.py) and
[`porchlight/ring/webhooks.py`](porchlight/ring/webhooks.py).

| Ring capability | Endpoint | Used for |
|---|---|---|
| Device discovery | `GET /v1/devices` | Find the doorbell |
| Event History | `GET /v1/history/devices/{id}/events` | New motion, ding and live-view events (works with a Playground token) |
| Image Snapshots | `POST /v1/devices/{id}/media/image/download` (`at_timestamp`, `latest_in_range`), then the 303 pre-signed URL | The frame for each event, and timed porch checks while a package is out |
| Webhooks v1.1 | `POST /webhooks/ring`, HMAC-SHA256 `X-Signature` over the raw body | `motion_detected`, `button_press`; deduplicated on `meta.request_id` |
| Live view | `POST /v1/devices/{id}/media/streaming/whep/sessions` | WebRTC live view in the dashboard; "Read this frame" sends a captured frame through the same gate |
| Media Clips | `POST /v1/devices/{id}/media/video/download` | Client support with Ring's 416/425 retry guidance |
| OAuth | `https://oauth.ring.com/oauth/token` | Refresh-token mode for a registered app |

A webhook and a history record describe the same moment under different ids, so events from the two
sources within 8 seconds of each other are treated as one.

---

## Run it

Requires Python 3.11+.

```bash
python -m venv .venv
```

```bash
.venv/Scripts/pip install -e ".[dev,aws]"
```

(On macOS or Linux use `.venv/bin/` instead of `.venv/Scripts/`.)

### With Ring (the Developer Playground)

1. Open the [Ring Developer Playground](https://developer.amazon.com/ring/console/playground) and press **Generate Token**.
2. Check the token and see what the Playground offers:

```bash
.venv/Scripts/python scripts/ring_check.py --token "eyJ..."
```

3. Copy `.env.example` to `.env`, set `RING_ACCESS_TOKEN`, and start:

```bash
.venv/Scripts/python -m porchlight
```

4. Open http://127.0.0.1:8000. Playground tokens last about 30 minutes; paste a new one from the **Ring** pill
   at the top right without restarting.

To have frames read automatically set `VISION_PROVIDER=bedrock` (with AWS credentials). Without it, each
frame appears under **Needs you** for you to confirm.

### Without Ring (local stand-in)

```bash
.venv/Scripts/python -m porchlight --demo
```

This starts a local stand-in for the Ring API at `/sim`, with drawn frames and a clock you can move
forward. It exists for tests and offline development. **It is not Ring and its frames are not Ring
footage**; the dashboard labels them as stand-in frames throughout.

### Tests

```bash
.venv/Scripts/python -m pytest -q
```

42 tests. The integration tests start the real server and exercise the Ring client, the signed webhook
path, the image-download redirect, the rules, and the MCP server over real HTTP against the stand-in.

---

## The MCP server (Alexa+ track)

`http://127.0.0.1:8000/mcp`, Streamable HTTP, built on the official MCP Python SDK
(negotiates protocol 2025-11-25 and later). Code: [`porchlight/mcp_server.py`](porchlight/mcp_server.py).

| Tool | What it does |
|---|---|
| `porch_status` | Today's check-ins, open alerts, pending requests, latest frame (returned as image content) |
| `get_evidence` | Each state change, the rule that made it, and the frames it rests on |
| `check_porch_now` | Fetch a fresh frame from Ring and read it |
| `list_check_ins`, `add_check_in`, `cancel_check_in` | Manage what is expected |
| `list_alerts`, `acknowledge_alert` | Alerts |
| `propose_action`, `list_proposed_actions` | Queue a request for the family. Never carries it out. |
| `decide_action` | Approve or decline. **MCP Apps visibility `["app"]`: only a tap on the card can call it; it is not offered to the model.** |

Four tools carry an MCP Apps card (`ui://porchlight/card.html`,
[`porchlight/web/mcp-card.html`](porchlight/web/mcp-card.html)): status, evidence, the live frame, and the
approve / not now buttons.

Connect any MCP client:

```json
{ "mcpServers": { "porchlight": { "type": "http", "url": "http://127.0.0.1:8000/mcp" } } }
```

Set `MCP_AUTH_TOKEN` to require a bearer token, and `PUBLIC_BASE_URL` when hosting.

### The simulated assistant

Hackathon participants cannot register a real Alexa+ add-on, so `/alexa` plays the host: it connects to
the MCP server over HTTP like any outside agent, lists tools, calls them, and renders the cards in a
sandboxed iframe using the MCP Apps postMessage protocol. Tool choice is a keyword router by default, or
a Bedrock model with `ASSISTANT_PROVIDER=bedrock`. The page says plainly that it is a simulation.

---

## AWS

| Service | Where | What for |
|---|---|---|
| Amazon Bedrock (Converse API, image input, forced tool use) | [`porchlight/vision/bedrock.py`](porchlight/vision/bedrock.py) | Read each porch frame into a fixed JSON shape with a confidence |
| Amazon Bedrock (Converse API, tool use loop) | [`porchlight/assistant.py`](porchlight/assistant.py) | Choose MCP tools for a spoken question and phrase the answer |

Both are optional and off by default.

---

## Open source spin-off

The Ring client, webhook verification and the local stand-in are also published on their own as
[**ring-partner**](https://github.com/jacklachan/ring-partner-py), an unofficial Python client for the Ring
Partner API, so other teams do not have to rewrite them. Porchlight keeps its own copy in `porchlight/ring/`
so this repository runs with no extra install step.

## What has and has not been verified

- **Verified by the test suite and by hand:** everything above, end to end, against the local stand-in.
- **Verified against live Ring (Developer Playground, 8 October 2026):** device discovery, status,
  capabilities, Event History (the Playground's Package stream arrives as an `on_demand` event), the image
  snapshot for that event through the 303 download, and WHEP live view with "Read this frame". Image download
  answers `403 Requested time range is not within authorized boundaries` for a window with no events;
  Porchlight treats that as "no frame", not a bad token.
- **Not verified live:** webhooks (the Playground token comes with no signing key or webhook URL),
  refresh-token sign-in, and video clips.
- **Not yet verified against live Bedrock:** the request and response shapes follow the Converse API; the
  response parser is unit-tested with recorded shapes, not a live call.

## Limits

- Ring's API serves US-located devices; outside the US the Developer Playground is the route.
- A visit is confirmed by a doorbell press, or by a trusted frame showing a person and no package. A courier
  is deliberately not counted as a visitor, so a visitor who arrives carrying a parcel needs the bell.
- "Brought in" means a later trusted frame shows the porch clear. Porchlight does not know who took it.
- Approved "ask a neighbour" actions go to `NOTIFY_WEBHOOK_URL` if set. Without it they are recorded only,
  and the dashboard says so.

## Layout

```
porchlight/
  ring/        client.py  webhooks.py  simulator.py (local stand-in)
  vision/      base.py  bedrock.py  fixture.py
  engine/      policy.py (rules + ledger)  ingest.py (events -> frames -> readings)
  mcp_server.py   assistant.py   api.py   notify.py
  web/         index.html app.js app.css   alexa.html   mcp-card.html
scripts/ring_check.py
tests/
```

## License

MIT. See [LICENSE](LICENSE).
