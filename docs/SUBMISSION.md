# Devpost submission draft

Deadline: **Friday 23 October 2026, 12:00 pm Pacific** (Saturday 24 October, 12:30 am IST).
Text in *[brackets]* must be replaced with the team's own experience before submitting.

## Tracks

- Primary track: **Ring**
- Also: **Alexa+** (self-hosted MCP server, Streamable HTTP, spec 2025-11-25 or later, plus a simulated host)
- Mini challenges: **AWS Builder** (Bedrock) *[only if Bedrock was actually used in the demo]*, **Open Source** (see below)

## Tagline

Care check-ins from the front door: Porchlight tells a family when Mom's delivery is still on the porch.

## What it does

Porchlight turns a Ring doorbell into a quiet check-in for a relative who lives alone. The family lists what
should happen (a pharmacy delivery between 2 and 4, a home aide at 9). Porchlight watches Ring events,
fetches the frame for each one, and checks the plan: did it arrive, was it brought in, did the aide come.
When something does not go to plan it says so in one specific sentence, with the frame attached.

A package left out all afternoon is not a security event, but it is often the first outside sign that
someone is unwell. That is the signal Porchlight is built around.

## How it works

- **Ring Partner API:** device discovery, Event History polling, image snapshots (`at_timestamp` and
  `latest_in_range` through the 303 pre-signed download), HMAC-verified v1.1 webhooks, and WHEP live view
  with in-browser frame capture.
- **A model advises, rules decide:** Amazon Bedrock reads each frame into a fixed shape (package, person,
  vehicle, confidence). Deterministic rules move each check-in. Readings under the confidence threshold are
  held until a family member confirms them. Every change is logged with its rule id and evidence ids.
- **Nothing acts without a person:** Porchlight can propose "ask Mrs Rao to knock". It is carried out only
  after someone approves.
- **MCP server for assistants:** 11 tools over Streamable HTTP. Four return MCP Apps cards. The approve
  tool has MCP Apps visibility `["app"]`, so it can be reached from a tap on the card and is never offered
  to the model.
- **Built to Ring's guidance:** the tiered vision funnel from Ring's computer vision guidelines (skip events
  that cannot matter, set aside unusable frames, reuse the last reading when the porch has not changed,
  call the model only for what is left), watermark-aware frame comparison, and the design guide's device,
  empty-state and accessibility rules.
- **Privacy:** no face recognition and no identification. The person at the door can pause watching or
  delete every frame; frames expire after 7 days; every view of a frame is logged by name.

## What was built during the hackathon

Everything. The repository's first commit is dated 8 October 2026.

## Product feedback (required, per tool)

Write this from real use. Starting points from the build are in [`FRICTION_LOG.md`](../FRICTION_LOG.md).

**Ring Partner API and Developer Playground**
- Used for: *[device discovery, Event History, image download, live view, webhooks if you had a signing key]*
- Worked well: *[...]*
- Needs work: no SDK or OpenAPI file; webhook and history ids cannot be correlated; one very large docs page; *[what you hit on the Playground]*
- Onboarding (zero to first API call): *[how long, what slowed you down]*
- Build with it again? *[Yes/No and why]*

**MCP (Python SDK, Streamable HTTP, MCP Apps)**
- Used for: the self-hosted server and the cards.
- Worked well: one decorator per tool; MCP Apps visibility let us make approval tap-only.
- Needs work: participants cannot test in Alexa+, so the host had to be simulated.
- Build with it again? *[...]*

**Amazon Bedrock** *[only if used]*
- Used for: reading frames (Converse API with image input and forced tool use); choosing tools in the assistant.
- *[model id, latency, quota or access problems on a new account]*

## Open Source mini challenge

- **Contribution URL / project repository:** https://github.com/jacklachan/ring-partner-py
- **GitHub username:** jacklachan
- **Created:** 8 October 2026, inside the hackathon window. MIT license.
- **What we did:** published `ring-partner`, an unofficial async Python client for the Ring Partner API,
  extracted from Porchlight.
- **How it works:** `RingClient` wraps device discovery, status, capabilities, paginated Event History,
  image snapshots (following the 303 pre-signed download without forwarding the bearer token), video clips
  with Ring's 416/425 retry guidance, and WHEP start/stop, with access-token and refresh-token sign-in.
  `parse` and `verify_signature` handle v1.1 webhooks. `ring-partner check` reports what a token can reach.
  `ring_partner.standin` is a local stand-in for the API (clearly not Ring) so apps can be tested with no
  token or network. 13 tests, CI on Python 3.10 and 3.13.
- **Why it matters:** Ring's docs state there is no official SDK, so every partner rewrites the same
  client before starting on their product. This removes that step and gives teams outside the US, who can
  only use the short-lived Playground token, a way to develop and test offline.
- **Be upfront on the form:** *[state whether it has been run against the live API yet; as of 8 October it had
  only been run against the stand-in]*

## Submission checklist

- [ ] Demo video under 3 minutes, public on YouTube or Vimeo, shows the project working through the Playground or a device
- [ ] Repo public with the MIT license visible in the About section
- [ ] Text description, tracks and mini challenges selected
- [ ] Product feedback for every tool used
- [ ] Friction log entries (up to 10% bonus): finish the TODO entries, remove any left unfilled
- [ ] `README.md` "What has and has not been verified" updated after the Playground run
- [ ] If the MCP endpoint is hosted, keep it up through judging (9 to 20 November)
- [ ] All three team members are 18 or older and added on Devpost
