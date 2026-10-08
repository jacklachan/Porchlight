"""The web app: JSON API for the family dashboard, Ring webhook receiver, and the MCP mount."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from . import __version__
from .assistant import Assistant
from .clock import MINUTE_MS, Clock
from .config import Settings
from .db import Store, new_id
from .engine import RULES, Ingest, Policy
from .mcp_server import build_mcp, parse_when
from .notify import WebhookNotifier
from .ring import MediaNotReady, RingAuthError, RingClient, RingError, WebhookError, parse, verify_signature
from .ring import simulator as sim
from .vision import build_vision

log = logging.getLogger("porchlight")
WEB_DIR = Path(__file__).parent / "web"
MAX_FRAME_BYTES = 6 * 1024 * 1024


class ExpectationIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    kind: str
    start_time: str
    end_time: str
    collect_within_minutes: int | None = Field(default=None, ge=1, le=24 * 60)


class PlanIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    kind: str
    start_time: str
    duration_minutes: int = Field(ge=5, le=12 * 60)
    repeat: str = "daily"
    collect_within_minutes: int | None = Field(default=None, ge=1, le=24 * 60)


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    role: str = Field(default="Neighbour", max_length=60)
    channel: str = Field(default="", max_length=120)


class ReviewIn(BaseModel):
    package_present: bool
    person_present: bool = False
    reviewer: str = Field(default="family", max_length=60)


class DecisionIn(BaseModel):
    approve: bool
    by: str = Field(default="family", max_length=60)


class AckIn(BaseModel):
    by: str = Field(default="family", max_length=60)


class TokenIn(BaseModel):
    token: str = Field(min_length=8)


class WhepIn(BaseModel):
    sdp_offer: str


class WhepStopIn(BaseModel):
    session_url: str


class AssistantIn(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    speaker: str = Field(default="family", max_length=60)


class CardCallIn(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AdvanceIn(BaseModel):
    minutes: int = Field(ge=1, le=24 * 60)


class SceneIn(BaseModel):
    scene: str
    event: str | None = "motion"


def create_app(settings: Settings | None = None, *, clock: Clock | None = None, start_background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    # The saved clock offset belongs to the stand-in's time controls. Against real Ring the clock is never shifted.
    saved_offset = int(store.get_meta("clock_offset_ms", "0") or 0) if settings.demo else 0
    clock = clock or Clock(saved_offset)
    notifier = WebhookNotifier(settings.notify_webhook_url)
    policy = Policy(store, clock, settings, notifier=notifier)
    ring = RingClient(
        access_token=settings.ring_access_token,
        refresh_token=settings.ring_refresh_token,
        client_id=settings.ring_client_id,
        client_secret=settings.ring_client_secret,
        api_base=settings.ring_api_base,
        oauth_url=settings.ring_oauth_url,
    )
    vision = build_vision(settings)
    ingest = Ingest(store, clock, settings, ring, vision, policy)
    mcp, _tools = build_mcp(policy, ingest)
    assistant = Assistant(settings, f"http://127.0.0.1:{settings.port}/mcp")

    sim_state: sim.SimState | None = None
    if settings.demo:
        sim_state = sim.SimState(
            clock=clock,
            signing_key=settings.ring_hmac_signing_key or "sim-signing-key",
            webhook_url=f"http://127.0.0.1:{settings.port}/webhooks/ring",
        )

    public_host = urlparse(settings.public_base_url).netloc if settings.public_base_url else None
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "testserver"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if public_host:
        allowed_hosts.append(public_host)
        allowed_origins.append(settings.public_base_url.rstrip("/"))  # type: ignore[union-attr]
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts, allowed_origins=allowed_origins
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        async with mcp.session_manager.run():
            if start_background:
                task = asyncio.create_task(ingest.run())
            try:
                yield
            finally:
                if task:
                    task.cancel()
                await ring.aclose()
                store.close()

    app = FastAPI(title="Porchlight", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.clock = clock
    app.state.policy = policy
    app.state.ingest = ingest
    app.state.ring = ring
    app.state.sim = sim_state
    app.state.mcp = mcp
    app.state.assistant = assistant

    # -- auth ----------------------------------------------------------------

    def _bearer_ok(request: Request, expected: str) -> bool:
        header = request.headers.get("authorization", "")
        return header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip(), expected)

    async def require_api(request: Request) -> None:
        if settings.api_token and not _bearer_ok(request, settings.api_token):
            raise HTTPException(status_code=401, detail="Missing or wrong dashboard token")

    @app.middleware("http")
    async def mcp_guard(request: Request, call_next: Any) -> Response:
        if request.url.path.startswith("/mcp") and settings.mcp_auth_token:
            if not _bearer_ok(request, settings.mcp_auth_token):
                return JSONResponse(
                    {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
        return await call_next(request)

    api = [Depends(require_api)]

    # -- shaping -------------------------------------------------------------

    def with_frame(obs: dict[str, Any] | None) -> dict[str, Any] | None:
        if obs and obs.get("snapshot_file"):
            obs["frame_url"] = f"/media/{obs['snapshot_file']}"
        return obs

    def full_status() -> dict[str, Any]:
        status = policy.status()
        status["last_observation"] = with_frame(status["last_observation"])
        status["review_queue"] = [with_frame(o) for o in status["review_queue"]]
        thumbs: dict[str, str] = {}
        for exp in status["expectations"]:
            for key in ("completion_observation", "arrival_observation"):
                obs = store.get("observations", exp[key]) if exp.get(key) else None
                if obs and obs.get("snapshot_file"):
                    thumbs[exp["id"]] = f"/media/{obs['snapshot_file']}"
                    break
        status["frames"] = thumbs
        status["plans"] = store.query("SELECT * FROM plans WHERE active = 1 ORDER BY start_minute")
        status["contacts"] = store.query("SELECT * FROM contacts ORDER BY rowid")
        status["tomorrow"] = store.query(
            "SELECT * FROM expectations WHERE state = 'scheduled' AND window_start >= ? AND window_start < ? "
            "ORDER BY window_start",
            [policy._day_start(status["now"]) + 24 * 60 * MINUTE_MS, policy._day_start(status["now"]) + 48 * 60 * MINUTE_MS],
        )
        status["recent_alerts"] = store.query("SELECT * FROM alerts ORDER BY ts DESC LIMIT 12")
        status["recent_actions"] = store.query(
            "SELECT * FROM actions WHERE status != 'proposed' ORDER BY decided_at DESC LIMIT 5"
        )
        status["connection"] = ingest.connection()
        status["vision"] = {
            "provider": vision.name,
            "model": settings.bedrock_model_id if vision.name == "bedrock" else None,
            "threshold": settings.confidence_threshold,
        }
        status["demo"] = {"enabled": settings.demo, "clock_offset_minutes": clock.offset_ms // MINUTE_MS}
        status["mcp_url"] = f"{settings.base_url}/mcp"
        return status

    # -- pages ---------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/alexa", include_in_schema=False)
    async def alexa_page() -> FileResponse:
        return FileResponse(WEB_DIR / "alexa.html")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/media/{name}", include_in_schema=False)
    async def media(name: str) -> FileResponse:
        path = (settings.media_dir / name).resolve()
        if path.parent != settings.media_dir.resolve() or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    # -- state ---------------------------------------------------------------

    @app.get("/api/status", dependencies=api)
    async def get_status() -> dict[str, Any]:
        return full_status()

    @app.get("/api/stream", dependencies=api)
    async def stream(request: Request) -> StreamingResponse:
        queue = ingest.subscribe()

        async def events() -> AsyncIterator[str]:
            try:
                yield ": connected\n\n"
                while not await request.is_disconnected():
                    try:
                        what = await asyncio.wait_for(queue.get(), timeout=20)
                        yield f"data: {what}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                ingest.unsubscribe(queue)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/evidence/{subject_id}", dependencies=api)
    async def evidence(subject_id: str) -> dict[str, Any]:
        chain = policy.evidence_for(subject_id)
        if not chain["subject"]:
            raise HTTPException(status_code=404, detail="No such check-in or alert")
        chain["observations"] = [with_frame(o) for o in chain["observations"]]
        return chain

    @app.get("/api/ledger", dependencies=api)
    async def ledger(limit: int = 60) -> dict[str, Any]:
        rows = store.query("SELECT * FROM ledger ORDER BY seq DESC LIMIT ?", [max(1, min(limit, 300))])
        for row in rows:
            row["rule"] = RULES.get(row["rule_id"] or "")
        return {"entries": rows, "rules": RULES}

    @app.get("/api/activity", dependencies=api)
    async def activity(limit: int = 30) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        observations = store.query("SELECT * FROM observations ORDER BY ts DESC LIMIT ?", [limit])
        events = store.query(
            "SELECT id, device_id, type, sub_type, ts, source FROM events ORDER BY ts DESC LIMIT ?", [limit]
        )
        return {"observations": [with_frame(o) for o in observations], "events": events}

    # -- care plan -----------------------------------------------------------

    @app.post("/api/expectations", dependencies=api)
    async def add_expectation(body: ExpectationIn) -> dict[str, Any]:
        now = clock.now_ms()
        try:
            start = parse_when(body.start_time, now, settings.timezone)
            end = parse_when(body.end_time, now, settings.timezone)
            exp = policy.add_expectation(
                body.title, body.kind, start, end, collect_within_min=body.collect_within_minutes
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await ingest.evaluate()
        return {"expectation": exp}

    @app.delete("/api/expectations/{exp_id}", dependencies=api)
    async def cancel_expectation(exp_id: str) -> dict[str, Any]:
        exp = policy.cancel_expectation(exp_id)
        if not exp:
            raise HTTPException(status_code=404)
        ingest.changed()
        return {"expectation": exp}

    @app.post("/api/plans", dependencies=api)
    async def add_plan(body: PlanIn) -> dict[str, Any]:
        now = clock.now_ms()
        try:
            start = parse_when(body.start_time, now, settings.timezone)
            start_minute = ((start - policy._day_start(start)) // MINUTE_MS) % 1440
            plan = policy.add_plan(
                body.title,
                body.kind,
                int(start_minute),
                body.duration_minutes,
                repeat=body.repeat,
                collect_within_min=body.collect_within_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ingest.changed()
        return {"plan": plan}

    @app.delete("/api/plans/{plan_id}", dependencies=api)
    async def remove_plan(plan_id: str) -> dict[str, Any]:
        policy.remove_plan(plan_id)
        ingest.changed()
        return {"ok": True}

    @app.post("/api/contacts", dependencies=api)
    async def add_contact(body: ContactIn) -> dict[str, Any]:
        contact = {"id": new_id("con"), "name": body.name, "role": body.role, "channel": body.channel}
        store.insert("contacts", contact)
        ingest.changed()
        return {"contact": contact}

    @app.delete("/api/contacts/{contact_id}", dependencies=api)
    async def remove_contact(contact_id: str) -> dict[str, Any]:
        store.execute("DELETE FROM contacts WHERE id = ?", [contact_id])
        ingest.changed()
        return {"ok": True}

    # -- review, alerts, actions ----------------------------------------------

    @app.post("/api/observations/{obs_id}/review", dependencies=api)
    async def review(obs_id: str, body: ReviewIn) -> dict[str, Any]:
        obs = policy.review_observation(
            obs_id, package_present=body.package_present, person_present=body.person_present, reviewer=body.reviewer
        )
        if not obs:
            raise HTTPException(status_code=404)
        await ingest.evaluate()
        return {"observation": with_frame(obs)}

    @app.post("/api/observations/{obs_id}/dismiss", dependencies=api)
    async def dismiss(obs_id: str, body: AckIn) -> dict[str, Any]:
        if not store.get("observations", obs_id):
            raise HTTPException(status_code=404)
        policy.dismiss_observation(obs_id, body.by)
        ingest.changed()
        return {"ok": True}

    @app.post("/api/observations/frame", dependencies=api)
    async def observe_frame(request: Request) -> dict[str, Any]:
        """A frame grabbed in the browser from the Ring live view (WHEP)."""
        content_type = request.headers.get("content-type", "image/jpeg").split(";")[0]
        if content_type not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(status_code=415, detail="Send a JPEG, PNG or WebP image body")
        content = await request.body()
        if not content or len(content) > MAX_FRAME_BYTES:
            raise HTTPException(status_code=413, detail="Frame is empty or too large")
        obs = await ingest.observe_frame(content, content_type, ts=clock.now_ms(), frame_source="ring_live_view")
        return {"observation": with_frame(obs)}

    @app.post("/api/porch/check", dependencies=api)
    async def porch_check() -> dict[str, Any]:
        try:
            obs = await ingest.check_porch_now()
        except MediaNotReady as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Ring has no stored frame for right now. Start Live view and press 'Read this frame', "
                    f"or wait for the next event. ({exc})"
                ),
            ) from exc
        except RingAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RingError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"observation": with_frame(obs)}

    @app.post("/api/alerts/{alert_id}/ack", dependencies=api)
    async def ack(alert_id: str, body: AckIn) -> dict[str, Any]:
        alert = policy.acknowledge_alert(alert_id, body.by)
        if not alert:
            raise HTTPException(status_code=404)
        ingest.changed()
        return {"alert": alert}

    @app.post("/api/actions/{action_id}/decide", dependencies=api)
    async def decide(action_id: str, body: DecisionIn) -> dict[str, Any]:
        action = policy.decide_action(action_id, approve=body.approve, by=body.by)
        if not action:
            raise HTTPException(status_code=404)
        await ingest.evaluate()
        return {"action": action}

    # -- ring ----------------------------------------------------------------

    @app.post("/webhooks/ring", include_in_schema=False)
    async def ring_webhook(request: Request) -> JSONResponse:
        raw = await request.body()
        key = settings.ring_hmac_signing_key
        if not key:
            return JSONResponse({"error": "RING_HMAC_SIGNING_KEY is not configured"}, status_code=503)
        if not verify_signature(key, raw, request.headers.get("x-signature")):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            event = parse(raw)
        except WebhookError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        outcome = ingest.accept_webhook(event)
        if outcome == "stored":
            # Ring expects an answer within 5 seconds; fetch and read the frame afterwards.
            task = asyncio.create_task(_observe_later(event.event_id))
            background.add(task)
            task.add_done_callback(background.discard)
        return JSONResponse({"status": outcome, "request_id": event.request_id})

    background: set[asyncio.Task[None]] = set()

    async def _observe_later(event_id: str) -> None:
        try:
            await ingest.observe_event(event_id)
            await ingest.evaluate()
        except (RingError, httpx.HTTPError) as exc:
            ingest.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("could not read frame for %s: %s", event_id, exc)

    @app.post("/api/ring/token", dependencies=api)
    async def set_token(body: TokenIn) -> dict[str, Any]:
        """Paste a fresh Playground token without restarting. Held in memory only."""
        ring.set_access_token(body.token.strip())
        ingest.device_name = None
        ingest.last_error = None
        try:
            await ingest.ensure_device()
        except RingAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except (RingError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        ingest.changed()
        return {"connection": ingest.connection()}

    @app.post("/api/ring/whep", dependencies=api)
    async def whep_start(body: WhepIn) -> dict[str, Any]:
        try:
            device_id = await ingest.ensure_device()
            answer, session_url = await ring.whep_start(device_id, body.sdp_offer)
        except RingAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except (RingError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"sdp_answer": answer, "session_url": session_url}

    @app.post("/api/ring/whep/stop", dependencies=api)
    async def whep_stop(body: WhepStopIn) -> dict[str, Any]:
        try:
            await ring.whep_stop(body.session_url)
        except (RingError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True}

    # -- simulated Alexa+ ------------------------------------------------------

    @app.post("/api/assistant", dependencies=api)
    async def assistant_turn(body: AssistantIn) -> dict[str, Any]:
        return await assistant.handle(body.text, body.speaker)

    @app.get("/api/assistant/card", dependencies=api)
    async def assistant_card(uri: str) -> Response:
        if not uri.startswith("ui://"):
            raise HTTPException(status_code=400, detail="Only ui:// resources")
        try:
            html = await assistant.read_card(uri)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(content=html, media_type="text/html")

    @app.post("/api/assistant/card-call", dependencies=api)
    async def assistant_card_call(body: CardCallIn) -> dict[str, Any]:
        try:
            return await assistant.call_from_card(body.name, body.arguments)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # -- demo controls (local stand-in only) -----------------------------------

    if sim_state is not None:
        app.include_router(sim.build_router(sim_state), prefix="/sim", include_in_schema=False)

        def require_stand_in() -> None:
            if "/sim" not in ring.api_base:
                raise HTTPException(
                    status_code=409,
                    detail="Time controls only work against the local stand-in. With real Ring, use short pickup windows instead.",
                )

        @app.post("/api/demo/advance", dependencies=api)
        async def advance(body: AdvanceIn) -> dict[str, Any]:
            require_stand_in()
            clock.advance(body.minutes * MINUTE_MS)
            store.set_meta("clock_offset_ms", str(clock.offset_ms))
            await ingest.tick()
            return {"now": clock.now_ms(), "clock_offset_minutes": clock.offset_ms // MINUTE_MS}

        @app.post("/api/demo/scene", dependencies=api)
        async def scene(body: SceneIn) -> dict[str, Any]:
            require_stand_in()
            result = await sim.trigger(sim_state, scene=body.scene, event=body.event)
            await ingest.tick()
            return result

        @app.post("/api/demo/reset", dependencies=api)
        async def reset() -> dict[str, Any]:
            require_stand_in()
            for table in ("events", "webhook_requests", "observations", "expectations", "alerts", "actions", "ledger", "plans"):
                store.execute(f"DELETE FROM {table}")
            sim_state.reset()
            clock.reset()
            store.set_meta("clock_offset_ms", "0")
            ingest.mark_history_cutoff()
            ingest.changed()
            return {"ok": True}

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    app.mount("/", mcp_app)
    return app
