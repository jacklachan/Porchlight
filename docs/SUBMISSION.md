# Devpost submission draft

Deadline: **Friday 23 October 2026, 12:00 pm Pacific** (Saturday 24 October, 12:30 am IST).
Text in *[brackets]* must be replaced with the team's own experience before submitting.

## Tracks

- Primary track: **Ring**
- Also: **Alexa+** (self-hosted MCP server, Streamable HTTP, spec 2025-11-25 or later, plus a simulated host)
- Mini challenges: **AWS Builder** (Bedrock) *[only if Bedrock was actually used in the demo]*, **Open Source** *[only once the separate contribution exists; see below]*

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
- **Privacy:** no face recognition and no identification; frames stay on the family's own server.

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

## Open Source mini challenge (optional, not done yet)

The rules ask for a new, additional open-source project or a contribution to a public repo. One honest
candidate: extract `porchlight/ring/` (the client, webhook verification and the local stand-in) into its
own small package, since Ring ships no SDK. Needs: its own repo with a license, README and tests, plus the
contribution URL, repo URL, GitHub username and a short description on the form.

## Submission checklist

- [ ] Demo video under 3 minutes, public on YouTube or Vimeo, shows the project working through the Playground or a device
- [ ] Repo public with the MIT license visible in the About section
- [ ] Text description, tracks and mini challenges selected
- [ ] Product feedback for every tool used
- [ ] Friction log entries (up to 10% bonus): finish the TODO entries, remove any left unfilled
- [ ] `README.md` "What has and has not been verified" updated after the Playground run
- [ ] If the MCP endpoint is hosted, keep it up through judging (9 to 20 November)
- [ ] All three team members are 18 or older and added on Devpost
