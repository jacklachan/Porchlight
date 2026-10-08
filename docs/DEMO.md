# Demo video plan (under 3 minutes)

The rules require the video to show the project **working through the Ring Developer Playground or a real
Ring device**. Record against the Playground, not the local stand-in. If you include any stand-in or
personal footage, say so on screen (Amazon's forum guidance: make clear which footage comes from Ring).

## Before recording

1. Fresh Playground token: `python scripts/ring_check.py --token "..."`. Confirm devices, history and a snapshot all say `ok`.
2. `.env`: `RING_ACCESS_TOKEN`, `VISION_PROVIDER=bedrock` (if Bedrock works on your account), `PORCHLIGHT_PERSON_NAME=Mom`.
3. Delete `data/` for a clean start, then `python -m porchlight`.
4. Add a contact (Mrs Rao, next-door neighbour) so the proposed action names a person.
5. Real Ring has no time controls. To show "still outside" quickly, create the delivery with the **15 minutes** pickup limit
   and cut the wait out of the recording. Do not fake the clock on real Ring footage.
6. Tokens last about 30 minutes: record in one sitting, or paste a new token from the Ring pill between takes.

## Shot list

| Time | On screen | Say |
|---|---|---|
| 0:00 to 0:15 | Dashboard, lamp amber, empty day | "My mother lives alone. A package that sits on her porch all afternoon is often the first sign something is wrong. Porchlight watches her Ring doorbell for that." |
| 0:15 to 0:35 | "Expect something": Pharmacy delivery, 2 to 4 pm, tell me after 15 minutes | "I tell it what should happen today." |
| 0:35 to 1:05 | Ring Developer Playground: trigger the **Package** stream. Back to the dashboard: check-in flips to "On the porch", frame appears | "Ring reports the event. Porchlight pulls the frame through Ring's image API and reads it with Bedrock. A rule, not the model, marks the delivery as arrived." |
| 1:05 to 1:30 | Alert: "still on the porch". Click **See the evidence**: rule id, frame, sha256, Ring event id | "Fifteen minutes later nobody has brought it in. Every alert shows the rule that fired and the exact frames behind it." |
| 1:30 to 1:50 | A low-confidence frame under "Your eyes needed"; click "Yes, a package" | "If the model is not sure, nothing changes until a person confirms. The model advises. It never acts." |
| 1:50 to 2:30 | `/alexa`: "How is Mom doing?" then "Ask a neighbour to knock." Card appears; tap **Yes, go ahead** | "The same data is an MCP server, so an assistant like Alexa+ can answer with the frame as proof. It can propose asking a neighbour, but approving is a button only a person can press. That tool is never offered to the model." |
| 2:30 to 2:50 | Open "How this was answered": tool calls, MCP version, Streamable HTTP. Then live view + "Read this frame" | "Under the hood: Ring Event History, snapshots, webhooks and WHEP live view; MCP over Streamable HTTP with MCP Apps cards." |
| 2:50 to 3:00 | Dashboard, lamp back to amber after pickup | "Porchlight. Care check-ins from the front door." |

## Things judges score, and where the video shows them

- **Tech implementation:** real Ring API calls (history, image download, live view), signed webhooks, MCP 2025-11-25+.
- **Design:** one sentence at the top answers "is she okay"; evidence one click away.
- **Impact:** caretaking is a Ring priority category and a listed Ring Appstore category.
- **Idea:** a non-security use of a security camera; absence of a pickup as a wellness signal.
