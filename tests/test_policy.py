"""The rules, one behaviour at a time."""

from conftest import at, reading

from porchlight.clock import HOUR_MS, MINUTE_MS


def observe(policy, clock, when, **kw):
    clock.set(when)
    obs = policy.record_observation(reading(**kw), ts=when, frame_source="test")
    policy.evaluate()
    return obs


def state(policy, exp):
    return policy.store.get("expectations", exp["id"])["state"]


def test_package_in_window_marks_delivery_arrived(policy, clock):
    exp = policy.add_expectation("Pharmacy", "delivery", at(14), at(16), collect_within_min=120)
    obs = observe(policy, clock, at(14, 14), package=True, person=True, vehicle=True)
    row = policy.store.get("expectations", exp["id"])
    assert row["state"] == "arrived"
    assert row["arrived_at"] == at(14, 14)
    assert row["arrival_observation"] == obs["id"]


def test_low_confidence_reading_changes_nothing_until_a_person_confirms(policy, clock):
    exp = policy.add_expectation("Pharmacy", "delivery", at(14), at(16))
    obs = observe(policy, clock, at(14, 30), package=True, confidence=0.4)
    assert obs["status"] == "needs_review"
    assert state(policy, exp) == "scheduled"

    policy.review_observation(obs["id"], package_present=True, person_present=False, reviewer="Asha")
    policy.evaluate()
    assert state(policy, exp) == "arrived"
    assert policy.store.get("observations", obs["id"])["status"] == "confirmed"


def test_failed_reading_goes_to_review(policy, clock):
    obs = observe(policy, clock, at(9), package=None, confidence=0.0, error="AccessDenied")
    assert obs["status"] == "needs_review"


def test_clear_porch_after_arrival_marks_collected(policy, clock):
    exp = policy.add_expectation("Pharmacy", "delivery", at(14), at(16), collect_within_min=120)
    observe(policy, clock, at(14, 14), package=True)
    observe(policy, clock, at(15, 2), package=False, person=True)
    row = policy.store.get("expectations", exp["id"])
    assert row["state"] == "completed"
    assert row["completed_at"] == at(15, 2)


def test_uncollected_package_raises_attention_then_urgent_with_a_proposal(policy, clock, alerts_sent):
    exp = policy.add_expectation("Pharmacy", "delivery", at(14), at(16), collect_within_min=120)
    observe(policy, clock, at(14, 14), package=True)

    clock.set(at(16, 13))
    policy.evaluate()
    assert state(policy, exp) == "arrived"
    assert alerts_sent == []

    clock.set(at(16, 15))
    policy.evaluate()
    assert state(policy, exp) == "overdue_pickup"
    assert [a["rule_id"] for a in alerts_sent] == ["delivery.uncollected"]
    assert "2:14 PM" in alerts_sent[0]["body"]
    assert alerts_sent[0]["evidence"], "an alert must cite the frames it rests on"

    clock.set(at(18, 20))
    policy.evaluate()
    policy.evaluate()  # idempotent: no duplicates
    assert [a["rule_id"] for a in alerts_sent] == ["delivery.uncollected", "delivery.uncollected.escalate"]
    actions = policy.store.query("SELECT * FROM actions")
    assert len(actions) == 1 and actions[0]["status"] == "proposed" and actions[0]["kind"] == "check_in"


def test_collection_resolves_alerts_and_withdraws_proposals(policy, clock):
    exp = policy.add_expectation("Pharmacy", "delivery", at(14), at(16), collect_within_min=60)
    observe(policy, clock, at(14, 14), package=True)
    clock.set(at(17))
    policy.evaluate()
    observe(policy, clock, at(17, 5), package=False)
    assert state(policy, exp) == "completed"
    assert policy.store.query("SELECT * FROM alerts WHERE state = 'open' AND level != 'info'") == []
    assert policy.store.query("SELECT * FROM actions WHERE status = 'proposed'") == []
    assert policy.status()["tone"] == "good"


def test_delivery_never_seen_is_missed_after_grace(policy, clock, alerts_sent):
    exp = policy.add_expectation("Meals", "delivery", at(12), at(13))
    clock.set(at(13, 59))
    policy.evaluate()
    assert state(policy, exp) == "scheduled"
    clock.set(at(14, 1))
    policy.evaluate()
    assert state(policy, exp) == "missed"
    assert alerts_sent[0]["rule_id"] == "delivery.missed"


def test_late_package_recovers_a_missed_delivery(policy, clock):
    exp = policy.add_expectation("Meals", "delivery", at(12), at(13))
    clock.set(at(14, 30))
    policy.evaluate()
    observe(policy, clock, at(15), package=True)
    assert state(policy, exp) == "arrived"
    assert policy.store.query("SELECT * FROM alerts WHERE rule_id = 'delivery.missed' AND state = 'open'") == []


def test_package_with_no_expectation_is_tracked_as_unplanned(policy, clock):
    observe(policy, clock, at(10), package=True)
    rows = policy.store.query("SELECT * FROM expectations")
    assert len(rows) == 1 and rows[0]["unplanned"] and rows[0]["state"] == "arrived"
    # the same package seen again does not create a second one
    observe(policy, clock, at(10, 20), package=True)
    assert len(policy.store.query("SELECT * FROM expectations")) == 1


def test_doorbell_press_confirms_a_visit(policy, clock):
    exp = policy.add_expectation("Home aide", "visit", at(9), at(10))
    clock.set(at(9, 12))
    policy.store.insert(
        "events",
        {
            "id": "evt1",
            "device_id": "d",
            "type": "ding",
            "ts": at(9, 12),
            "source": "webhook",
            "raw": {},
            "created_at": at(9, 12),
        },
    )
    policy.evaluate()
    row = policy.store.get("expectations", exp["id"])
    assert row["state"] == "completed" and row["completion_event"] == "evt1"


def test_a_courier_or_an_unsure_reading_does_not_confirm_a_visit(policy, clock):
    exp = policy.add_expectation("Home aide", "visit", at(9), at(10))
    observe(policy, clock, at(9, 5), package=True, person=True, vehicle=True)
    assert state(policy, exp) == "scheduled"
    observe(policy, clock, at(9, 30), package=None, person=True, confidence=0.2)
    assert state(policy, exp) == "scheduled"


def test_person_seen_confirms_visit(policy, clock):
    exp = policy.add_expectation("Home aide", "visit", at(9), at(10))
    observe(policy, clock, at(9, 40), package=False, person=True)
    assert state(policy, exp) == "completed"


def test_missed_visit(policy, clock, alerts_sent):
    exp = policy.add_expectation("Home aide", "visit", at(9), at(10))
    clock.set(at(11, 1))
    policy.evaluate()
    assert state(policy, exp) == "missed"
    assert alerts_sent[0]["rule_id"] == "visit.missed"


def test_recurring_plan_materialises_once_per_day(policy, clock):
    policy.add_plan("Meals", "delivery", 12 * 60, 60, repeat="daily")
    policy.evaluate()
    policy.evaluate()
    rows = policy.store.query("SELECT * FROM expectations ORDER BY window_start")
    assert [r["window_start"] for r in rows] == [at(12), at(12, day=13)]


def test_plan_made_after_its_window_does_not_create_an_instant_miss(policy, clock):
    clock.set(at(17))
    policy.add_plan("Morning aide", "visit", 9 * 60, 60, repeat="daily")
    rows = policy.store.query("SELECT * FROM expectations")
    assert [r["window_start"] for r in rows] == [at(9, day=13)]


def test_weekday_plan_skips_the_weekend(policy, clock):
    clock.set(at(8, day=16))  # Friday
    policy.add_plan("Aide", "visit", 9 * 60, 60, repeat="weekdays")
    rows = policy.store.query("SELECT * FROM expectations")
    assert [r["window_start"] for r in rows] == [at(9, day=16)]


def test_actions_need_a_person(policy, clock):
    action = policy.propose_action(kind="check_in", title="Ask Mrs Rao to knock", proposed_by="assistant")
    assert action["status"] == "proposed"
    decided = policy.decide_action(action["id"], approve=True, by="Asha")
    assert decided["status"] == "approved" and decided["decided_by"] == "Asha"
    # a second decision does not overwrite the first
    again = policy.decide_action(action["id"], approve=False, by="Ravi")
    assert again["status"] == "approved"


def test_evidence_chain_links_alert_to_frames_and_rules(policy, clock, alerts_sent):
    policy.add_expectation("Pharmacy", "delivery", at(14), at(16), collect_within_min=60)
    obs = observe(policy, clock, at(14, 14), package=True)
    clock.set(at(15, 30))
    policy.evaluate()
    chain = policy.evidence_for(alerts_sent[0]["id"])
    assert [o["id"] for o in chain["observations"]] == [obs["id"]]
    rules = [e["rule_id"] for e in chain["ledger"] if e["rule_id"]]
    assert rules == ["delivery.arrived", "delivery.uncollected"]


def test_status_headline_reflects_urgency(policy, clock):
    assert policy.status()["tone"] == "quiet"
    policy.add_expectation("Pharmacy", "delivery", at(14), at(16), collect_within_min=60)
    observe(policy, clock, at(14, 14), package=True)
    clock.set(at(14, 14) + 3 * HOUR_MS + MINUTE_MS)
    policy.evaluate()
    status = policy.status()
    assert status["tone"] == "urgent"
    assert len(status["proposed_actions"]) == 1


def test_frames_confirmed_out_of_order_do_not_invent_a_second_delivery(policy, clock):
    exp = policy.add_expectation("Pharmacy", "delivery", at(14), at(16))
    clock.set(at(14, 30))
    early = policy.record_observation(reading(package=True, confidence=0.1), ts=at(14, 10), frame_source="test")
    late = policy.record_observation(reading(package=True, confidence=0.1), ts=at(14, 20), frame_source="test")

    policy.review_observation(late["id"], package_present=True, person_present=False, reviewer="Asha")
    policy.evaluate()
    policy.review_observation(early["id"], package_present=True, person_present=False, reviewer="Asha")
    policy.evaluate()

    rows = policy.store.query("SELECT * FROM expectations")
    assert len(rows) == 1 and rows[0]["id"] == exp["id"]
    assert rows[0]["arrived_at"] == at(14, 10) and rows[0]["arrival_observation"] == early["id"]
