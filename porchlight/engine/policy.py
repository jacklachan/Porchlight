"""The policy gate: deterministic rules that turn evidence into care-plan changes.

A vision model can describe a frame, but it cannot change anything by itself.
The only things that move an expectation from one state to another are:

* a trusted observation (model confidence at or above the threshold, or a
  reading a family member confirmed by hand),
* a doorbell press reported by Ring, or
* the clock.

Every change is written to the ledger with the rule that made it and the
observation or event ids it rests on.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from ..clock import DAY_MS, MINUTE_MS, Clock, fmt_duration, fmt_time, to_local, tz
from ..config import Settings
from ..db import Store, new_id
from ..vision.base import Reading

OPEN_STATES = ("scheduled", "arrived", "overdue_pickup")
TRUSTED = ("trusted", "confirmed")
DOORBELL_TYPES = ("ding", "button_press")

RULES: dict[str, str] = {
    "frame.unusable": "The frame was too dark, blown out or blank to say anything about the porch.",
    "frame.unchanged": "The porch looks the same as in the last frame that was read, so that reading still stands.",
    "event.skipped": "Nothing was expected and a frame was read recently, or the motion was an animal: no model call.",
    "evidence.gate": "A reading counts only at or above the confidence threshold, or once a person confirms it.",
    "delivery.arrived": "A trusted frame shows a package inside an expected delivery window (with grace).",
    "delivery.arrived_late": "A trusted frame shows a package after the window closed, the same day.",
    "delivery.unexpected": "A trusted frame shows a package that matches no expected delivery.",
    "delivery.collected": "A trusted frame after arrival shows the porch clear.",
    "delivery.missed": "The window and its grace period ended with no package seen.",
    "delivery.uncollected": "The package has been out longer than the agreed pickup time.",
    "delivery.uncollected.escalate": "The package has been out more than twice the agreed pickup time.",
    "visit.rang": "The doorbell was pressed inside an expected visit window.",
    "visit.seen": "A trusted frame shows a person, with no package, inside an expected visit window.",
    "visit.missed": "The window and its grace period ended with no visitor seen.",
    "privacy.paused": "Watching was paused from the dashboard. No events or frames are taken while paused.",
    "privacy.retention": "Frames are deleted after the retention period. The reading and the frame's hash are kept.",
    "action.approved": "A family member approved a proposed action.",
    "action.rejected": "A family member rejected a proposed action.",
}

_RECORDED = {
    "trusted": "observation recorded",
    "needs_review": "observation held for review",
    "unusable": "frame set aside as unusable",
}
_RECORD_RULE = {"unusable": "frame.unusable"}

# Called with an alert or an approved action. May return a sentence saying where it was sent.
Notifier = Callable[[dict[str, Any]], str | None]


class Policy:
    def __init__(self, store: Store, clock: Clock, settings: Settings, notifier: Notifier | None = None) -> None:
        self.store = store
        self.clock = clock
        self.settings = settings
        self.notifier = notifier

    # -- care plan -------------------------------------------------------

    def add_expectation(
        self,
        title: str,
        kind: str,
        window_start: int,
        window_end: int,
        *,
        collect_within_min: int | None = None,
        plan_id: str | None = None,
        actor: str = "family",
        note: str | None = None,
    ) -> dict[str, Any] | None:
        if kind not in ("delivery", "visit"):
            raise ValueError("kind must be 'delivery' or 'visit'")
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        if kind == "delivery" and not collect_within_min:
            collect_within_min = self.settings.default_collect_within_minutes
        now = self.clock.now_ms()
        exp_id = new_id("exp")
        created = self.store.insert(
            "expectations",
            {
                "id": exp_id,
                "plan_id": plan_id,
                "title": title.strip() or kind.title(),
                "kind": kind,
                "window_start": int(window_start),
                "window_end": int(window_end),
                "collect_within_min": collect_within_min if kind == "delivery" else None,
                "state": "scheduled",
                "note": note,
                "created_at": now,
                "updated_at": now,
            },
            ignore=True,
        )
        if not created:
            return None
        self.store.record(now, actor, exp_id, "check-in added", detail={"title": title, "kind": kind})
        return self.store.get("expectations", exp_id)

    def cancel_expectation(self, exp_id: str, actor: str = "family") -> dict[str, Any] | None:
        exp = self.store.get("expectations", exp_id)
        if not exp or exp["state"] in ("completed", "cancelled"):
            return exp
        now = self.clock.now_ms()
        self.store.update("expectations", exp_id, {"state": "cancelled", "updated_at": now})
        self._resolve_for(exp_id, now)
        self.store.record(now, actor, exp_id, "check-in cancelled")
        return self.store.get("expectations", exp_id)

    def add_plan(
        self,
        title: str,
        kind: str,
        start_minute: int,
        duration_min: int,
        *,
        repeat: str = "daily",
        weekday: int | None = None,
        collect_within_min: int | None = None,
        actor: str = "family",
    ) -> dict[str, Any]:
        if kind not in ("delivery", "visit"):
            raise ValueError("kind must be 'delivery' or 'visit'")
        if repeat not in ("daily", "weekdays", "weekly"):
            raise ValueError("repeat must be daily, weekdays or weekly")
        if not (0 <= start_minute < 1440) or duration_min <= 0:
            raise ValueError("start_minute must be within the day and duration positive")
        now = self.clock.now_ms()
        if repeat == "weekly" and weekday is None:
            weekday = to_local(now, self.settings.timezone).weekday()
        plan_id = new_id("plan")
        self.store.insert(
            "plans",
            {
                "id": plan_id,
                "title": title.strip() or kind.title(),
                "kind": kind,
                "start_minute": start_minute,
                "duration_min": duration_min,
                "collect_within_min": collect_within_min,
                "repeat": repeat,
                "weekday": weekday,
                "active": True,
                "created_at": now,
            },
        )
        self.store.record(now, actor, plan_id, "recurring plan added", detail={"title": title, "repeat": repeat})
        self.materialize_plans(now)
        return self.store.get("plans", plan_id)  # type: ignore[return-value]

    def remove_plan(self, plan_id: str, actor: str = "family") -> None:
        now = self.clock.now_ms()
        self.store.update("plans", plan_id, {"active": False})
        for exp in self.store.query(
            "SELECT id FROM expectations WHERE plan_id = ? AND state = 'scheduled' AND window_start > ?",
            [plan_id, now],
        ):
            self.cancel_expectation(exp["id"], actor)
        self.store.record(now, actor, plan_id, "recurring plan removed")

    def materialize_plans(self, now: int) -> None:
        """Create today's and tomorrow's expectations from the recurring plans."""
        zone = tz(self.settings.timezone)
        today = to_local(now, self.settings.timezone).replace(hour=0, minute=0, second=0, microsecond=0)
        for plan in self.store.query("SELECT * FROM plans WHERE active = 1"):
            for offset in (0, 1):
                day = today + timedelta(days=offset)
                weekday = day.weekday()
                if plan["repeat"] == "weekdays" and weekday > 4:
                    continue
                if plan["repeat"] == "weekly" and weekday != plan["weekday"]:
                    continue
                start_local = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(
                    minutes=plan["start_minute"]
                )
                start = int(start_local.timestamp() * 1000)
                end = start + plan["duration_min"] * MINUTE_MS
                if end <= plan["created_at"]:
                    continue  # do not invent a window that was already over when the plan was made
                self.add_expectation(
                    plan["title"],
                    plan["kind"],
                    start,
                    end,
                    collect_within_min=plan["collect_within_min"],
                    plan_id=plan["id"],
                    actor="plan",
                )

    # -- pausing and deleting ----------------------------------------------

    FOREVER = 2**62

    def paused_until(self) -> int | None:
        """Epoch ms the pause ends (FOREVER for 'until resumed'), or None when watching."""
        until = int(self.store.get_meta("paused_until", "0") or 0)
        if until and until > self.clock.now_ms():
            return until
        if until:
            self._end_pause("policy", "pause ended")
        return None

    def pause(self, minutes: int | None, by: str) -> int:
        now = self.clock.now_ms()
        until = now + minutes * MINUTE_MS if minutes else self.FOREVER
        self.store.set_meta("paused_until", str(until))
        self.store.set_meta("paused_since", str(now))
        self.store.record(
            now, by, "porchlight", "watching paused", rule_id="privacy.paused", detail={"minutes": minutes}
        )
        return until

    def resume(self, by: str) -> None:
        if int(self.store.get_meta("paused_until", "0") or 0):
            self._end_pause(by, "watching resumed")

    def _end_pause(self, by: str, what: str) -> None:
        now = self.clock.now_ms()
        since = int(self.store.get_meta("paused_since", "0") or 0)
        self.store.set_meta("paused_until", "0")
        self.store.record(now, by, "porchlight", what, rule_id="privacy.paused")
        # Anything whose window overlapped the pause was not watched. Say so, rather than calling it missed.
        for exp in self.store.query(
            "SELECT * FROM expectations WHERE state = 'scheduled' AND window_start < ? AND window_end > ?", [now, since]
        ):
            self.store.update(
                "expectations",
                exp["id"],
                {"state": "cancelled", "note": "Not watched: Porchlight was paused", "updated_at": now},
            )
            self.store.record(now, "policy", exp["id"], "not watched: paused during its window", rule_id="privacy.paused")

    def delete_frames(self, media_dir: Any, *, older_than_ms: int | None, by: str) -> int:
        """Delete stored frame files. Readings, hashes and the ledger stay, so the record still makes sense."""
        if older_than_ms is None:
            rows = self.store.query("SELECT id, snapshot_file FROM observations WHERE snapshot_file IS NOT NULL")
        else:
            rows = self.store.query(
                "SELECT id, snapshot_file FROM observations WHERE snapshot_file IS NOT NULL AND ts < ?", [older_than_ms]
            )
        files = {row["snapshot_file"] for row in rows}
        for name in files:
            try:
                (media_dir / name).unlink(missing_ok=True)
            except OSError:
                pass
        for row in rows:
            self.store.update("observations", row["id"], {"snapshot_file": None})
        if rows:
            # A frame nobody answered about cannot be answered once it is gone.
            self.store.execute("UPDATE observations SET status = 'expired' WHERE status = 'needs_review' AND snapshot_file IS NULL")
            self.store.record(
                self.clock.now_ms(),
                by,
                "porchlight",
                f"{len(files)} frame{'s' if len(files) != 1 else ''} deleted",
                rule_id="privacy.retention" if older_than_ms is not None else None,
            )
        return len(files)

    # -- observations ----------------------------------------------------

    def gate(self, reading: Reading) -> str:
        """rule evidence.gate: is this reading allowed to act without a person?"""
        if reading.error or reading.package_present is None:
            return "needs_review"
        if reading.confidence >= self.settings.confidence_threshold:
            return "trusted"
        return "needs_review"

    def record_observation(
        self,
        reading: Reading,
        *,
        ts: int,
        frame_source: str,
        event_id: str | None = None,
        device_id: str | None = None,
        snapshot_file: str | None = None,
        snapshot_sha256: str | None = None,
        fingerprint: str | None = None,
        carried_from: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        obs_id = new_id("obs")
        status = status or self.gate(reading)
        now = self.clock.now_ms()
        self.store.insert(
            "observations",
            {
                "id": obs_id,
                "event_id": event_id,
                "device_id": device_id,
                "ts": ts,
                "frame_source": frame_source,
                "snapshot_file": snapshot_file,
                "snapshot_sha256": snapshot_sha256,
                "fingerprint": fingerprint,
                "carried_from": carried_from,
                "provider": reading.provider,
                "model": reading.model,
                "package_present": reading.package_present,
                "person_present": reading.person_present,
                "vehicle_present": reading.vehicle_present,
                "confidence": reading.confidence,
                "summary": reading.summary,
                "status": status,
                "created_at": now,
            },
        )
        self.store.record(
            now,
            f"vision:{reading.provider}",
            obs_id,
            _RECORDED.get(status, "observation recorded"),
            rule_id=_RECORD_RULE.get(status, "frame.unchanged" if carried_from else "evidence.gate"),
            evidence=[e for e in (event_id, carried_from) if e],
            detail={
                "confidence": reading.confidence,
                "threshold": self.settings.confidence_threshold,
                "status": status,
                "error": reading.error,
            },
        )
        return self.store.get("observations", obs_id)  # type: ignore[return-value]

    def review_observation(
        self,
        obs_id: str,
        *,
        package_present: bool,
        person_present: bool,
        reviewer: str,
    ) -> dict[str, Any] | None:
        """A person looks at the frame and says what is there. Their answer is trusted."""
        obs = self.store.get("observations", obs_id)
        if not obs:
            return None
        now = self.clock.now_ms()
        self.store.update(
            "observations",
            obs_id,
            {
                "package_present": package_present,
                "person_present": person_present,
                "status": "confirmed",
                "reviewed_by": reviewer,
                "reviewed_at": now,
                "applied": False,
            },
        )
        self.store.record(
            now,
            reviewer,
            obs_id,
            "observation confirmed by a person",
            rule_id="evidence.gate",
            detail={
                "package_present": package_present,
                "person_present": person_present,
                "model_said": {"package_present": obs["package_present"], "confidence": obs["confidence"]},
            },
        )
        return self.store.get("observations", obs_id)

    def dismiss_observation(self, obs_id: str, reviewer: str) -> None:
        now = self.clock.now_ms()
        self.store.update(
            "observations", obs_id, {"status": "dismissed", "reviewed_by": reviewer, "reviewed_at": now}
        )
        self.store.record(now, reviewer, obs_id, "observation dismissed (frame unusable)")

    # -- evaluation ------------------------------------------------------

    def evaluate(self) -> list[dict[str, Any]]:
        """Apply every rule. Safe to call as often as you like; returns the new alerts."""
        now = self.clock.now_ms()
        new_alerts: list[dict[str, Any]] = []
        if self.paused_until():
            return new_alerts  # nothing is watched and nothing is judged while paused
        self.materialize_plans(now)

        for event in self.store.query(
            "SELECT * FROM events WHERE applied = 0 AND type IN (?, ?) ORDER BY ts", list(DOORBELL_TYPES)
        ):
            self._apply_doorbell(event, new_alerts)
            self.store.execute("UPDATE events SET applied = 1 WHERE id = ?", [event["id"]])

        for obs in self.store.query(
            "SELECT * FROM observations WHERE applied = 0 AND status IN (?, ?) ORDER BY ts", list(TRUSTED)
        ):
            self._apply_observation(obs, new_alerts)
            self.store.update("observations", obs["id"], {"applied": True})

        self._apply_clock(now, new_alerts)

        # Review requests go stale: a day-old frame says nothing about the porch now.
        self.store.execute(
            "UPDATE observations SET status = 'expired' WHERE status = 'needs_review' AND ts < ?", [now - DAY_MS]
        )
        return new_alerts

    def _grace(self) -> tuple[int, int]:
        return self.settings.early_grace_minutes * MINUTE_MS, self.settings.late_grace_minutes * MINUTE_MS

    def _apply_doorbell(self, event: dict[str, Any], new_alerts: list[dict[str, Any]]) -> None:
        early, late = self._grace()
        visit = self.store.one(
            "SELECT * FROM expectations WHERE kind = 'visit' AND state IN ('scheduled', 'missed') "
            "AND window_start - ? <= ? AND window_end + ? >= ? ORDER BY window_end LIMIT 1",
            [early, event["ts"], late, event["ts"]],
        )
        if visit:
            self._complete_visit(visit, "visit.rang", event["ts"], [event["id"]], event_id=event["id"])
            self._alert(
                new_alerts,
                level="info",
                rule_id="visit.rang",
                title=f"{visit['title']}: arrived",
                body=f"The doorbell rang at {self._t(event['ts'])}, inside the expected window.",
                expectation_id=visit["id"],
                evidence=[event["id"]],
                dedupe=f"visit.done:{visit['id']}",
                ts=event["ts"],
            )

    def _apply_observation(self, obs: dict[str, Any], new_alerts: list[dict[str, Any]]) -> None:
        early, late = self._grace()
        ts = obs["ts"]

        if obs["package_present"] is True:
            exp = self.store.one(
                "SELECT * FROM expectations WHERE kind = 'delivery' AND state = 'scheduled' "
                "AND window_start - ? <= ? AND window_end + ? >= ? ORDER BY window_end LIMIT 1",
                [early, ts, late, ts],
            )
            rule = "delivery.arrived"
            if not exp:
                day_start = self._day_start(ts)
                exp = self.store.one(
                    "SELECT * FROM expectations WHERE kind = 'delivery' AND state = 'missed' "
                    "AND window_end < ? AND window_end >= ? ORDER BY window_end DESC LIMIT 1",
                    [ts, day_start],
                )
                rule = "delivery.arrived_late"
            already_out = self.store.one(
                "SELECT * FROM expectations WHERE kind = 'delivery' AND state IN ('arrived', 'overdue_pickup') "
                "ORDER BY arrived_at LIMIT 1"
            )
            if exp:
                self._mark_arrived(exp, rule, obs, new_alerts)
            elif not already_out:
                self._unexpected_delivery(obs, new_alerts)
            elif ts < already_out["arrived_at"]:
                # Frames can be confirmed out of order. An earlier sighting of the package that is
                # already out moves its arrival time back; it is not a second delivery.
                self.store.update(
                    "expectations",
                    already_out["id"],
                    {"arrived_at": ts, "arrival_observation": obs["id"], "updated_at": self.clock.now_ms()},
                )
                self.store.record(
                    self.clock.now_ms(),
                    "policy",
                    already_out["id"],
                    "arrival time moved earlier",
                    rule_id="delivery.arrived",
                    evidence=[obs["id"]],
                )
            # else: the same package, still waiting. Nothing changes.

        elif obs["package_present"] is False:
            waiting = self.store.query(
                "SELECT * FROM expectations WHERE kind = 'delivery' AND state IN ('arrived', 'overdue_pickup') "
                "AND arrived_at < ?",
                [ts],
            )
            for exp in waiting:
                self._mark_collected(exp, obs, new_alerts)

        if obs["person_present"] is True and obs["package_present"] is not True:
            visit = self.store.one(
                "SELECT * FROM expectations WHERE kind = 'visit' AND state IN ('scheduled', 'missed') "
                "AND window_start - ? <= ? AND window_end + ? >= ? ORDER BY window_end LIMIT 1",
                [early, ts, late, ts],
            )
            if visit:
                self._complete_visit(visit, "visit.seen", ts, [obs["id"]], observation_id=obs["id"])
                self._alert(
                    new_alerts,
                    level="info",
                    rule_id="visit.seen",
                    title=f"{visit['title']}: arrived",
                    body=f"Someone was at the door at {self._t(ts)}, inside the expected window.",
                    expectation_id=visit["id"],
                    evidence=[obs["id"]],
                    dedupe=f"visit.done:{visit['id']}",
                    ts=ts,
                )

    def _apply_clock(self, now: int, new_alerts: list[dict[str, Any]]) -> None:
        _, late = self._grace()
        name = self.settings.person_name

        for exp in self.store.query("SELECT * FROM expectations WHERE state = 'scheduled' AND window_end + ? < ?", [late, now]):
            rule = f"{exp['kind']}.missed"
            self.store.update("expectations", exp["id"], {"state": "missed", "updated_at": now})
            self.store.record(now, "policy", exp["id"], "marked missed", rule_id=rule)
            window = f"{self._t(exp['window_start'])} to {self._t(exp['window_end'])}"
            if exp["kind"] == "delivery":
                body = f"Expected between {window}. No package has been seen at the door."
            else:
                body = f"Expected between {window}. Nobody has been seen at the door and the bell has not rung."
            self._alert(
                new_alerts,
                level="attention",
                rule_id=rule,
                title=f"{exp['title']}: not seen",
                body=body,
                expectation_id=exp["id"],
                evidence=[],
                dedupe=f"missed:{exp['id']}",
                ts=now,
            )

        for exp in self.store.query(
            "SELECT * FROM expectations WHERE kind = 'delivery' AND state IN ('arrived', 'overdue_pickup')"
        ):
            limit = (exp["collect_within_min"] or self.settings.default_collect_within_minutes) * MINUTE_MS
            waited = now - exp["arrived_at"]
            evidence = [e for e in (exp["arrival_observation"], self._latest_sighting(exp, now)) if e]
            evidence = list(dict.fromkeys(evidence))
            if exp["state"] == "arrived" and waited >= limit:
                self.store.update("expectations", exp["id"], {"state": "overdue_pickup", "updated_at": now})
                self.store.record(
                    now, "policy", exp["id"], "pickup overdue", rule_id="delivery.uncollected", evidence=evidence
                )
                self._alert(
                    new_alerts,
                    level="attention",
                    rule_id="delivery.uncollected",
                    title=f"{exp['title']}: still on the porch",
                    body=(
                        f"It arrived at {self._t(exp['arrived_at'])} and has not been brought in "
                        f"{fmt_duration(waited)} later. {name} usually collects within "
                        f"{fmt_duration(limit)}."
                    ),
                    expectation_id=exp["id"],
                    evidence=evidence,
                    dedupe=f"uncollected:{exp['id']}",
                    ts=now,
                )
            if waited >= 2 * limit:
                alert = self._alert(
                    new_alerts,
                    level="urgent",
                    rule_id="delivery.uncollected.escalate",
                    title=f"{exp['title']}: still not brought in",
                    body=(
                        f"It arrived at {self._t(exp['arrived_at'])} and was still out {fmt_duration(waited)} "
                        f"later, more than twice as long as usual. It may be worth checking that {name} is okay."
                    ),
                    expectation_id=exp["id"],
                    evidence=evidence,
                    dedupe=f"uncollected.escalate:{exp['id']}",
                    ts=now,
                )
                if alert:
                    self.store.record(
                        now,
                        "policy",
                        exp["id"],
                        "escalated to urgent",
                        rule_id="delivery.uncollected.escalate",
                        evidence=evidence,
                    )
                    # The urgent alert replaces the earlier nudge rather than sitting beside it.
                    self.store.execute(
                        "UPDATE alerts SET state = 'resolved' WHERE dedupe_key = ? AND state != 'resolved'",
                        [f"uncollected:{exp['id']}"],
                    )
                    self._propose_check_in(exp, alert, waited)

    # -- transitions -----------------------------------------------------

    def _mark_arrived(
        self, exp: dict[str, Any], rule: str, obs: dict[str, Any], new_alerts: list[dict[str, Any]]
    ) -> None:
        now = self.clock.now_ms()
        self.store.update(
            "expectations",
            exp["id"],
            {"state": "arrived", "arrived_at": obs["ts"], "arrival_observation": obs["id"], "updated_at": now},
        )
        self.store.record(now, "policy", exp["id"], "marked arrived", rule_id=rule, evidence=[obs["id"]])
        self.store.execute(
            "UPDATE alerts SET state = 'resolved' WHERE expectation_id = ? AND rule_id = 'delivery.missed' "
            "AND state = 'open'",
            [exp["id"]],
        )
        late = " (after the expected window)" if rule == "delivery.arrived_late" else ""
        self._alert(
            new_alerts,
            level="info",
            rule_id=rule,
            title=f"{exp['title']}: arrived",
            body=f"A package was seen on the porch at {self._t(obs['ts'])}{late}.",
            expectation_id=exp["id"],
            evidence=[obs["id"]],
            dedupe=f"arrived:{exp['id']}",
            ts=obs["ts"],
        )

    def _unexpected_delivery(self, obs: dict[str, Any], new_alerts: list[dict[str, Any]]) -> None:
        now = self.clock.now_ms()
        exp_id = new_id("exp")
        self.store.insert(
            "expectations",
            {
                "id": exp_id,
                "title": "Unplanned delivery",
                "kind": "delivery",
                "window_start": obs["ts"],
                "window_end": obs["ts"] + MINUTE_MS,
                "collect_within_min": self.settings.default_collect_within_minutes,
                "state": "arrived",
                "unplanned": True,
                "arrived_at": obs["ts"],
                "arrival_observation": obs["id"],
                "created_at": now,
                "updated_at": now,
            },
        )
        self.store.record(
            now, "policy", exp_id, "unplanned delivery tracked", rule_id="delivery.unexpected", evidence=[obs["id"]]
        )
        self._alert(
            new_alerts,
            level="info",
            rule_id="delivery.unexpected",
            title="A package nobody was expecting",
            body=f"Seen on the porch at {self._t(obs['ts'])}. Porchlight will watch for it being brought in.",
            expectation_id=exp_id,
            evidence=[obs["id"]],
            dedupe=f"arrived:{exp_id}",
            ts=obs["ts"],
        )

    def _mark_collected(self, exp: dict[str, Any], obs: dict[str, Any], new_alerts: list[dict[str, Any]]) -> None:
        now = self.clock.now_ms()
        self.store.update(
            "expectations",
            exp["id"],
            {
                "state": "completed",
                "completed_at": obs["ts"],
                "completion_observation": obs["id"],
                "updated_at": now,
            },
        )
        evidence = [e for e in (exp["arrival_observation"], obs["id"]) if e]
        self.store.record(now, "policy", exp["id"], "marked collected", rule_id="delivery.collected", evidence=evidence)
        self._resolve_for(exp["id"], now)
        self._alert(
            new_alerts,
            level="info",
            rule_id="delivery.collected",
            title=f"{exp['title']}: brought in",
            body=(
                f"The porch was clear at {self._t(obs['ts'])}, "
                f"{fmt_duration(obs['ts'] - exp['arrived_at'])} after it arrived."
            ),
            expectation_id=exp["id"],
            evidence=evidence,
            dedupe=f"collected:{exp['id']}",
            ts=obs["ts"],
        )

    def _complete_visit(
        self,
        exp: dict[str, Any],
        rule: str,
        ts: int,
        evidence: list[str],
        *,
        event_id: str | None = None,
        observation_id: str | None = None,
    ) -> None:
        now = self.clock.now_ms()
        self.store.update(
            "expectations",
            exp["id"],
            {
                "state": "completed",
                "arrived_at": ts,
                "completed_at": ts,
                "completion_event": event_id,
                "completion_observation": observation_id,
                "updated_at": now,
            },
        )
        self.store.record(now, "policy", exp["id"], "visit confirmed", rule_id=rule, evidence=evidence)
        self._resolve_for(exp["id"], now)

    def _resolve_for(self, exp_id: str, now: int) -> None:
        self.store.execute(
            "UPDATE alerts SET state = 'resolved' WHERE expectation_id = ? AND state = 'open' AND level != 'info'",
            [exp_id],
        )
        self.store.execute(
            "UPDATE actions SET status = 'withdrawn', decided_at = ?, result = 'No longer needed' "
            "WHERE expectation_id = ? AND status = 'proposed'",
            [now, exp_id],
        )

    # -- alerts and actions ------------------------------------------------

    def _alert(
        self,
        new_alerts: list[dict[str, Any]],
        *,
        level: str,
        rule_id: str,
        title: str,
        body: str,
        expectation_id: str | None,
        evidence: list[str],
        dedupe: str,
        ts: int,
    ) -> dict[str, Any] | None:
        alert_id = new_id("alt")
        created = self.store.insert(
            "alerts",
            {
                "id": alert_id,
                "ts": ts,
                "level": level,
                "rule_id": rule_id,
                "title": title,
                "body": body,
                "expectation_id": expectation_id,
                "evidence": evidence,
                "state": "open",
                "dedupe_key": dedupe,
            },
            ignore=True,
        )
        if not created:
            return None
        alert = self.store.get("alerts", alert_id)
        assert alert is not None
        new_alerts.append(alert)
        if self.notifier and level != "info":
            self.notifier({"type": "alert", **alert})
        return alert

    def acknowledge_alert(self, alert_id: str, by: str) -> dict[str, Any] | None:
        alert = self.store.get("alerts", alert_id)
        if not alert:
            return None
        if alert["state"] == "open":
            now = self.clock.now_ms()
            self.store.update("alerts", alert_id, {"state": "acknowledged", "ack_by": by, "ack_at": now})
            self.store.record(now, by, alert_id, "alert acknowledged")
        return self.store.get("alerts", alert_id)

    def _propose_check_in(self, exp: dict[str, Any], alert: dict[str, Any], waited: int) -> None:
        contact = self.store.one("SELECT * FROM contacts ORDER BY rowid LIMIT 1")
        who = contact["name"] if contact else "a neighbour"
        self.propose_action(
            kind="check_in",
            title=f"Ask {who} to knock on the door",
            detail=(
                f"{exp['title']} has been on the porch for {fmt_duration(waited)}. "
                f"Ask {who} to check that {self.settings.person_name} is okay."
            ),
            reason=RULES["delivery.uncollected.escalate"],
            proposed_by="policy",
            expectation_id=exp["id"],
            alert_id=alert["id"],
            dedupe=f"check_in:{exp['id']}",
        )

    def propose_action(
        self,
        *,
        kind: str,
        title: str,
        detail: str = "",
        reason: str = "",
        proposed_by: str,
        expectation_id: str | None = None,
        alert_id: str | None = None,
        dedupe: str | None = None,
    ) -> dict[str, Any] | None:
        """Queue something for a person to approve. Nothing is carried out here."""
        now = self.clock.now_ms()
        action_id = new_id("act")
        created = self.store.insert(
            "actions",
            {
                "id": action_id,
                "ts": now,
                "kind": kind,
                "title": title,
                "detail": detail,
                "reason": reason,
                "proposed_by": proposed_by,
                "expectation_id": expectation_id,
                "alert_id": alert_id,
                "status": "proposed",
                "dedupe_key": dedupe,
            },
            ignore=True,
        )
        if not created:
            return None
        self.store.record(now, proposed_by, action_id, "action proposed", detail={"kind": kind, "title": title})
        return self.store.get("actions", action_id)

    def decide_action(self, action_id: str, *, approve: bool, by: str, result: str | None = None) -> dict[str, Any] | None:
        action = self.store.get("actions", action_id)
        if not action or action["status"] != "proposed":
            return action
        now = self.clock.now_ms()
        status = "approved" if approve else "rejected"
        if approve and result is None:
            result = self._carry_out(action, by)
        self.store.update(
            "actions", action_id, {"status": status, "decided_by": by, "decided_at": now, "result": result}
        )
        self.store.record(
            now, by, action_id, f"action {status}", rule_id=f"action.{status}", detail={"result": result}
        )
        return self.store.get("actions", action_id)

    def _carry_out(self, action: dict[str, Any], by: str) -> str:
        """Runs only after a person approved. Returns a plain sentence about what happened."""
        exp = self.store.get("expectations", action["expectation_id"]) if action["expectation_id"] else None
        if action["kind"] == "reschedule":
            if not exp:
                return "Nothing to reschedule: the check-in no longer exists."
            moved = self.add_expectation(
                exp["title"],
                exp["kind"],
                exp["window_start"] + DAY_MS,
                exp["window_end"] + DAY_MS,
                collect_within_min=exp["collect_within_min"],
                actor=by,
                note=f"Rescheduled from {exp['id']}",
            )
            if exp["state"] in ("scheduled", "missed"):
                self.cancel_expectation(exp["id"], actor=by)
            if not moved:
                return "Could not reschedule."
            day = to_local(moved["window_start"], self.settings.timezone).strftime("%A")
            return f"Moved to {day}, {self._t(moved['window_start'])} to {self._t(moved['window_end'])}."
        contact = self.store.one("SELECT * FROM contacts ORDER BY rowid LIMIT 1")
        sent = None
        if self.notifier:
            sent = self.notifier({"type": "action", "approved_by": by, "contact": contact, **action})
        return sent or "Approved and recorded. No outbound channel is configured, so nobody was messaged."

    # -- reading the state -------------------------------------------------

    def _t(self, ms: int) -> str:
        return fmt_time(ms, self.settings.timezone)

    def _day_start(self, ms: int) -> int:
        local = to_local(ms, self.settings.timezone).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(local.timestamp() * 1000)

    def _latest_sighting(self, exp: dict[str, Any], now: int) -> str | None:
        row = self.store.one(
            "SELECT id FROM observations WHERE status IN (?, ?) AND package_present = 1 AND ts >= ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            [*TRUSTED, exp["arrived_at"], now],
        )
        return row["id"] if row else None

    def attention_window(self, ts: int) -> bool:
        """True when a frame at this time could matter: something is expected around now, or a package is out."""
        early, late = self._grace()
        if self.needs_porch_check():
            return True
        return bool(
            self.store.one(
                "SELECT id FROM expectations WHERE state IN ('scheduled', 'missed') "
                "AND window_start - ? <= ? AND window_end + ? >= ? LIMIT 1",
                [early, ts, late, ts],
            )
        )

    def frame_stats(self) -> dict[str, int]:
        """Today's funnel: how many frames arrived, and how few needed a model."""
        start = self._day_start(self.clock.now_ms())
        rows = self.store.query(
            "SELECT provider, status, COUNT(*) AS n FROM observations WHERE ts >= ? GROUP BY provider, status", [start]
        )
        stats = {"frames": 0, "unchanged": 0, "unusable": 0, "read_by_model": 0, "confirmed_by_person": 0}
        for row in rows:
            stats["frames"] += row["n"]
            if row["provider"] == "unchanged":
                stats["unchanged"] += row["n"]
            elif row["status"] == "unusable":
                stats["unusable"] += row["n"]
            elif row["provider"] not in ("none", "gate"):
                stats["read_by_model"] += row["n"]
            if row["status"] == "confirmed":
                stats["confirmed_by_person"] += row["n"]
        skipped = self.store.one("SELECT COUNT(*) AS n FROM ledger WHERE rule_id = 'event.skipped' AND ts >= ?", [start])
        stats["events_skipped"] = skipped["n"] if skipped else 0
        return stats

    def needs_porch_check(self) -> bool:
        """True while a package is out: the ingest loop then looks at the porch on a timer."""
        return bool(
            self.store.one("SELECT id FROM expectations WHERE kind = 'delivery' AND state IN ('arrived', 'overdue_pickup') LIMIT 1")
        )

    def today(self) -> list[dict[str, Any]]:
        """Today's expectations, plus anything older that is still open."""
        now = self.clock.now_ms()
        start = self._day_start(now)
        return self.store.query(
            "SELECT * FROM expectations WHERE state != 'cancelled' AND "
            "((window_start >= ? AND window_start < ?) OR (window_start < ? AND state IN ('arrived', 'overdue_pickup'))) "
            "ORDER BY window_start",
            [start, start + DAY_MS, start],
        )

    def status(self) -> dict[str, Any]:
        now = self.clock.now_ms()
        items = self.today()
        open_alerts = self.store.query(
            "SELECT * FROM alerts WHERE state = 'open' AND level != 'info' ORDER BY "
            "CASE level WHEN 'urgent' THEN 0 ELSE 1 END, ts DESC"
        )
        review = self.store.query("SELECT * FROM observations WHERE status = 'needs_review' ORDER BY ts DESC LIMIT 20")
        actions = self.store.query("SELECT * FROM actions WHERE status = 'proposed' ORDER BY ts DESC")
        last_obs = self.store.one("SELECT * FROM observations ORDER BY ts DESC LIMIT 1")
        done = sum(1 for i in items if i["state"] == "completed")
        name = self.settings.person_name

        paused = self.paused_until()
        if paused:
            tone = "quiet"
            headline = (
                "Porchlight is paused."
                if paused >= self.FOREVER
                else f"Porchlight is paused until {self._t(paused)}."
            )
        elif any(a["level"] == "urgent" for a in open_alerts):
            tone, headline = "urgent", open_alerts[0]["title"]
        elif open_alerts:
            tone, headline = "attention", open_alerts[0]["title"]
        elif any(i["state"] in ("overdue_pickup", "missed") for i in items):
            # Acknowledging an alert does not make the problem go away: keep saying it until it is resolved.
            first = next(i for i in items if i["state"] in ("overdue_pickup", "missed"))
            tone = "attention"
            if first["state"] == "overdue_pickup":
                headline = f"{first['title']} is still on the porch."
            else:
                headline = f"{first['title']} has not been seen."
        elif not items:
            tone, headline = "quiet", f"Nothing is expected at {name}'s door today."
        elif done == len(items):
            tone = "good"
            headline = "Today's check-in is done." if len(items) == 1 else f"All {len(items)} of today's check-ins are done."
        else:
            waiting = [i for i in items if i["state"] == "arrived"]
            if waiting:
                tone, headline = "good", f"{waiting[0]['title']} has arrived and is waiting to be brought in."
            else:
                tone, headline = "good", f"On track: {done} of {len(items)} check-ins done so far today."

        return {
            "now": now,
            "paused_until": paused,
            "paused_forever": bool(paused and paused >= self.FOREVER),
            "timezone": self.settings.timezone,
            "person_name": name,
            "tone": tone,
            "headline": headline,
            "expectations": items,
            "alerts": open_alerts,
            "review_queue": review,
            "proposed_actions": actions,
            "last_observation": last_obs,
        }

    def evidence_for(self, subject_id: str) -> dict[str, Any]:
        """The receipts for an expectation or alert: ledger entries plus the observations and events they cite."""
        subject = self.store.get("expectations", subject_id) or self.store.get("alerts", subject_id)
        ids: list[str] = []
        ledger: list[dict[str, Any]] = []
        if subject:
            exp_id = subject.get("expectation_id") or subject["id"]
            ledger = self.store.query("SELECT * FROM ledger WHERE subject = ? ORDER BY seq", [exp_id])
            for entry in ledger:
                ids.extend(entry["evidence"])
            ids.extend(subject.get("evidence") or [])
        ids = list(dict.fromkeys(ids))
        observations = [o for o in (self.store.get("observations", i) for i in ids) if o]
        events = [e for e in (self.store.get("events", i) for i in ids) if e]
        for obs in observations:
            if obs["event_id"] and all(e["id"] != obs["event_id"] for e in events):
                event = self.store.get("events", obs["event_id"])
                if event:
                    events.append(event)
        for entry in ledger:
            entry["rule"] = RULES.get(entry["rule_id"] or "", None)
        return {"subject": subject, "ledger": ledger, "observations": observations, "events": events}
