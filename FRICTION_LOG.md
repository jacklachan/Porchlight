# Friction log

Format per entry: task, steps, expected vs actual, severity, workaround, suggestion.

Entries 1 to 6 were hit while building Porchlight's Ring client and MCP server **from the published
documentation and the hello-world sample, before running against a Playground token**. Entries marked
TODO are for the team to fill in from real Playground sessions; do not submit a TODO entry.

---

## 1. No SDK, so every partner re-implements the same client

- **Task:** call device discovery, Event History and image download from Python.
- **Steps:** read "Testing Your Integration" in the Ring API docs looking for a client library.
- **Expected:** an official client, or at least an OpenAPI file to generate one from.
- **Actual:** "There is no official Ring Partner SDK." The hello-world sample has one-file scripts per endpoint, but no reusable client, no types, no retry helpers.
- **Severity:** Medium. About 300 lines of client code before any product work.
- **Workaround:** wrote `porchlight/ring/client.py` (auth modes, JSON:API unwrapping, the 303 download flow, 416/425 retry policy, WHEP start/stop).
- **Suggestion:** publish an OpenAPI description of `api.amazonvision.com`. Typed clients in any language then come for free, and the MCP server for AI assistants could be generated from the same source.

## 2. Image and video download: the redirect step is easy to get subtly wrong

- **Task:** download the snapshot for an event.
- **Steps:** `POST /v1/devices/{id}/media/image/download`, get `303 See Other`, follow `Location`.
- **Expected:** guidance on whether the bearer token should be sent to the pre-signed URL.
- **Actual:** the docs say clients "can either follow the redirect automatically or extract the Location header". Most HTTP libraries that auto-follow a POST 303 will forward the `Authorization` header to the download host unless it is a different origin, and behaviour differs by library. The docs do not say whether the download host accepts, ignores or rejects that header.
- **Severity:** Low to medium (possible token leakage to a storage host, or a confusing 400).
- **Workaround:** disabled auto-redirect and fetch `Location` by hand with no `Authorization` header.
- **Suggestion:** state explicitly "do not send your bearer token to the Location URL" and show the two-request form as the primary example.

## 3. Event History timestamps: documented type is ambiguous

- **Task:** parse `attributes.start` / `attributes.end` from Event History.
- **Steps:** read the "Response (200 OK)" example and the "Response fields" table.
- **Expected:** one type.
- **Actual:** the example shows a string, `"start": "<timestamp> (e.g., 1699457230000)"`, while the field table says `number` (epoch milliseconds). The pagination cursor in the same response is an ISO 8601 string.
- **Severity:** Low.
- **Workaround:** accept numbers and numeric strings (`_as_ms` in the client).
- **Suggestion:** make the example a real JSON number, and say why the cursor uses a different time format.

## 4. The same moment has two unrelated ids (webhook vs Event History)

- **Task:** use webhooks when available and Event History as a fallback, without double counting.
- **Steps:** compare a `motion_detected` webhook (`data.id` = `<device_id>_<sub_type>_<timestamp>`) with the history record for the same motion (`id` is an opaque "directed event ID").
- **Expected:** a shared id, or a documented way to correlate.
- **Actual:** the docs only suggest correlating "by time window" (in the multi-camera section).
- **Severity:** Medium. Any app that reconciles the two sources must invent a tolerance.
- **Workaround:** treat events from the two sources on the same device within 8 seconds as one.
- **Suggestion:** include the history event id in the webhook payload (for example `data.attributes.history_event_id`), or document a recommended tolerance.

## 5. One very large documentation page

- **Task:** find the request body for image download and the webhook signature rules.
- **Steps:** open `developer.amazon.com/docs/ring/api-documentation.html`.
- **Expected:** a page per topic.
- **Actual:** the page is close to 1 MB of HTML containing the whole reference; section names such as "Image Snapshots" appear four or more times (nav, overview, body, cross-links), so in-page search lands in the wrong place repeatedly. AI coding assistants that fetch the page get truncated content.
- **Severity:** Low, but it slows every lookup.
- **Workaround:** saved the page locally and searched the text.
- **Suggestion:** one page per endpoint group, plus a plain-text or Markdown export (an `llms.txt`) for coding assistants.

## 6. Alexa+ track: required tech cannot be exercised by participants

- **Task:** test a self-hosted MCP server inside Alexa+.
- **Steps:** looked for a way to register the server with Alexa+; read the hackathon forum.
- **Expected:** a simulator or a sandbox account for participants.
- **Actual:** forum replies from Amazon confirm the add-on toolkit and web simulator are for preview partners only. Every team has to build its own host to demo.
- **Severity:** Medium for the track: submissions cannot show Alexa+ behaviour, only their guess at it.
- **Workaround:** built a small host (`/alexa`) that connects over Streamable HTTP and implements the MCP Apps postMessage handshake so cards render the way a real host would render them.
- **Suggestion:** publish a reference "Alexa+ like" MCP host (even a static web page) with the card sizes, supported MCP Apps features and voice-response length limits, so teams build to the real constraints.

## 7. Image download returns 403 for a window with no events, with an auth-shaped status

- **Task:** fetch the most recent frame from the Playground device ("what does the porch look like now").
- **Steps:** fresh Playground token; `GET /v1/devices` ok; Event History returned 0 events; then
  `POST /v1/devices/{id}/media/image/download` with `{"type": "latest_in_range", "start_timestamp": now - 10 min}`.
- **Expected:** an image, or `416 MEDIA_NOT_FOUND` as the Media docs describe for "no media".
- **Actual:** `403` with "Requested time range is not within authorized boundaries". The Image Snapshots page
  says timestamps must be "within last 180 days", and does not mention this error or what the authorized
  boundary is (consent time? event windows?).
- **Severity:** Medium. A 403 reads as "bad token", so a client that maps 401/403 to re-authentication
  (ours did) tells the user to get a new token when the token is fine.
- **Workaround:** treat a 403 whose message mentions the time range as "no media", and retry with a
  one-minute window.
- **Suggestion:** document the authorized boundary on the Image Snapshots page, give the error a stable
  `code`, and consider 416 for consistency with the other "no media" cases.

## 8. "Latest snapshot" with a live session open returns 422 about a decryption key

- **Task:** fetch the most recent frame while a live view was open on the Playground device.
- **Steps:** `POST /v1/devices/{id}/media/image/download` with `{"type": "latest_in_range", "start_timestamp": now - 10 min}`,
  a minute after an `on_demand` event whose `at_timestamp` snapshot had downloaded fine.
- **Expected:** the latest frame, or a documented "no media" status (416 or 425).
- **Actual:** `422` with the message "No valid Greco key available for decryption". "Greco" is not
  mentioned anywhere in the public docs, and 422 is not listed for this endpoint.
- **Severity:** Medium. The message reads like an internal fault and gives a developer nothing to act on. We
  could not tell whether it means encrypted media, a recording still in progress, or a Playground limitation.
- **Workaround:** treat a 422 mentioning decryption as "no usable media" and fall back to a frame captured
  from the WHEP live view.
- **Suggestion:** return a documented status and stable error code for unreadable media (the computer
  vision guidelines already tell apps to expect it for encrypted devices), and keep internal names out of
  the message.

---

## TODO (fill in from real Playground sessions)

- **Playground token lifetime.** Did the ~30 minute expiry interrupt a session? How did the API report it (status code, error body)? Was the dashboard's "Ring needs a new token" state enough?
- **Image download on the Playground device.** Does `at_timestamp` return a frame for a simulated Package / Vehicle / Motion event? Does `latest_in_range` work with no recent event? Record the exact status and error code if not.
- **Event History on the Playground.** Confirmed 8 Oct: the Package stream shows up as `event_type: on_demand` with no detection subtype, so an app cannot tell Package from Vehicle from Motion without its own vision. Write this up as an entry.
- **WHEP live view.** Did the browser offer produce an answer first time? Any 500s?
- **Bedrock.** Model access, quotas on a new account, latency per frame.
- **Onboarding.** Time from "open developer.ring.com" to first successful API call.
