# Porchlight: team handoff

Written 8 October 2026. Deadline: **Friday 23 October, 12:00 pm Pacific (Saturday 24 October, 12:30 am IST).**

## What we are submitting

Porchlight turns a Ring doorbell into a check-in for a parent who lives alone. The family says what should
happen today (a pharmacy delivery from 2 to 4, an aide at 9). Porchlight watches Ring, reads the frames, and
says so when something does not go to plan: "Pharmacy delivery arrived at 2:25 PM and has not been brought
in 15 minutes later."

- **Primary track:** Ring (caretaking, a non-security use)
- **Also entered:** Alexa+ (a self-hosted MCP server plus a simulated assistant page)
- **Mini challenge:** Open Source, with [ring-partner-py](https://github.com/jacklachan/ring-partner-py)
- **Not entering:** AWS Builder, unless someone gets Bedrock working (see "Decisions")

Repos: [Porchlight](https://github.com/jacklachan/Porchlight) and
[ring-partner-py](https://github.com/jacklachan/ring-partner-py). Both public, MIT.

## Where things stand

| | Status |
|---|---|
| Code | Built. 50 tests pass, CI green. |
| Real Ring (Developer Playground) | Works: devices, event history, snapshots, live view. A full run on 8 Oct went from live view to an automatic "still on the porch" alert with no clicks. |
| Frame reading | Works with Gemini (`gemini-3.8-flash`): 95 to 99% confidence on real Ring frames, about 5 seconds each. |
| Dashboard, voice page (`/alexa`), TV page (`/tv`) | Work, tested by clicking through. |
| Demo video | **Not started.** |
| Devpost submission text and product feedback | **Drafted with gaps.** See `docs/SUBMISSION.md`. |
| Friction log | 8 entries written, TODO section still to finish. See `FRICTION_LOG.md`. |

Not yet tested on real Ring (fixed in code after the last live run, need one more pass):
- "Look now" while a live view is open. It used to fail with a Ring 422 error.
- Snapshots at live-view size, so the same porch is not sent to the model twice.
- The urgent alert and the "ask a neighbour" approval, which fire at twice the pickup limit.

Never tested live at all: Ring webhooks (the Playground gives no signing key), Bedrock, and the TV page on an
actual Fire TV.

## Run it on your machine

Needs Python 3.11 or newer.

```bash
git clone https://github.com/jacklachan/Porchlight
```

```bash
cd Porchlight
```

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\pip install -e ".[dev]"
```

macOS or Linux:

```bash
.venv/bin/pip install -e ".[dev]"
```

**Try it with no accounts at all** (a built-in stand-in for Ring, with drawn frames and a clock you can move):

```bash
.venv\Scripts\python -m porchlight --demo
```

Open http://127.0.0.1:8000 and use the "Stand-in controls" panel. This mode is for development only.
**It must not appear in the demo video**; the rules require real Ring or the Playground.

**Run it against real Ring:**

1. Copy `.env.example` to `.env` and set the vision lines (each person uses their own key; never commit `.env`):

```
VISION_PROVIDER=openai
VISION_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
VISION_MODEL=gemini-3.8-flash
VISION_API_KEY=your-own-gemini-key
```

2. Start it: `.venv\Scripts\python -m porchlight`
3. Get a token at https://developer.amazon.com/ring/console/playground (Generate Token).
4. Open http://127.0.0.1:8000, click the **Connect Ring** pill, paste the token.
5. Click **Expect something**, add a delivery covering the current time with a 15-minute limit.
6. Click **Live view**, tick **Keep watching**. The delivery should flip to "On the porch" within 30 seconds.

Without a vision key it still works: each frame appears under "Needs you" for a yes or no.

Tests: `.venv\Scripts\python -m pytest -q`

## Things that will trip you up

- **Playground tokens last about 30 minutes** and are held in memory only. Restarting Porchlight loses the
  token. Paste a new one from the pill; no restart needed.
- **The Playground only produced events while a live view was open.** Use Live view plus Keep watching.
- **There is no clock control on real Ring.** To show "still on the porch" in the video, use the 15-minute
  pickup limit and cut the wait.
- **Port 8000 in use** means a copy is already running. Use `--port 8001`.
- **Old data from a previous run** lives in the `data/` folder. Delete it for a clean start.
- **A fix to the Ring client has to be made twice:** in `porchlight/ring/` and in the separate
  `ring-partner-py` repo.

## What is left, in priority order

1. **One more live pass** to confirm the three untested fixes above. About 45 minutes including the wait.
2. **Record the demo video** (under 3 minutes, public on YouTube or Vimeo, English). Shot list is in
   `docs/DEMO.md`. Before recording, check the forum thread "Playground questions: demo video and offline
   device": another team asked whether Ring-branded footage may appear in a public video, and it was
   unanswered when we last looked.
3. **Finish the friction log.** Replace the TODO section with entries from your own sessions, or delete it.
   Good candidates we already saw: the Playground event arrives as `on_demand` with no detail, and tokens
   expire mid-session. It is worth up to a 10% judging bonus.
4. **Fill in `docs/SUBMISSION.md`.** The bracketed parts need your own words, especially product feedback
   for each tool: what worked, what did not, how onboarding felt, would you build with it again.
5. **Submit on Devpost.** Add all three of us to the project. Everyone must be 18 or older.
6. **Optional: Fire TV.** `/tv` is a web page sized for a TV and driven by a remote. Wrapping it in a small
   Android WebView app and running it on the Fire TV simulator would let us also enter the Fire TV track.
   Needs Android Studio. Do this only if items 1 to 5 are done.

Suggested split: one person on the live pass and video, one on the friction log and submission text, one on
the Fire TV wrapper or on review and polish.

## Decisions already made, and why

- **Ring as the primary track.** 51,000 people registered. Fire TV and Alexa+ have almost no barrier to entry;
  Ring needs real API work, so it is likely less crowded.
- **Gemini instead of Bedrock for vision.** We have no AWS account, credits ran out on 7 October, and the forum
  has reports of new accounts with zero Bedrock quota. The code supports Bedrock (`VISION_PROVIDER=bedrock`)
  if anyone gets access, which would also qualify us for the AWS Builder mini challenge.
- **The model advises, rules decide.** A frame reading only changes anything at 75% confidence or after a
  person confirms it. This is the main idea of the project; keep it when changing things.
- **Care, not surveillance.** No face recognition. The person at the door can pause watching or delete all
  frames, and every view of a frame is logged. Ring's content policy bans covert monitoring, and Ring
  engineers are judging.
- **Built to Ring's own guidance.** The README maps our features to Ring's computer vision guidelines, design
  guide and content policy. New features should fit that table.
- **Hosting is not required.** The FAQ says a locally runnable repo plus the video is enough.

## Where to look in the code

```
porchlight/
  ring/client.py        every Ring API call
  ring/webhooks.py      webhook signature check and parsing
  ring/simulator.py     the local stand-in (not Ring)
  engine/policy.py      the rules, alerts and the record of every change
  engine/ingest.py      events in, frames fetched, cheap checks, model call
  vision/               frame checks, Gemini/OpenAI-compatible, Bedrock
  mcp_server.py         the 11 tools an assistant can call
  assistant.py          the simulated assistant behind /alexa
  api.py                web server and dashboard API
  web/                  dashboard, /alexa, /tv, and the assistant card
docs/DEMO.md            video shot list
docs/SUBMISSION.md      Devpost text, checklist
FRICTION_LOG.md         friction entries for the bonus
scripts/ring_check.py   what a Ring token can reach
scripts/vision_check.py test the vision model on one image
```

## Useful links

- Hackathon: https://amazonappdev2026.devpost.com/ ([rules](https://amazonappdev2026.devpost.com/rules), [FAQ](https://amazonappdev2026.devpost.com/details/faqs), [forum](https://amazonappdev2026.devpost.com/forum_topics))
- Ring Playground: https://developer.amazon.com/ring/console/playground
- Ring API docs: https://developer.amazon.com/docs/ring/api-documentation.html
- Ring computer vision guidelines: https://developer.amazon.com/docs/ring/computer-vision-guidelines.html
- Office hours 2: Monday 19 October, 9:00 am Pacific (link on the hackathon Updates tab)
