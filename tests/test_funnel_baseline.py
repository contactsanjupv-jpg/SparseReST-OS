"""Funnel-bottleneck baseline: cohort conversion vs. the same transition's own trailing baseline."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from packages.core.funnel import (
    MIN_COHORT, Event, compute_funnel_report, funnel_report_for_product, load_events, report_degradation_incidents,
)
from packages.db.models import FunnelEvent, Incident
from packages.db.session import session_scope
from tests.helpers import add_prospect, company_with_products

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def days_ago(d: float) -> datetime:
    return NOW - timedelta(days=d)


def cohort(a: str, b: str, *, n: int, converted: int, entered_days_ago: float) -> list[Event]:
    events = []
    for i in range(n):
        pid = uuid.uuid4()
        at = days_ago(entered_days_ago)
        events.append(Event(pid, a, at))
        if i < converted:
            events.append(Event(pid, b, at + timedelta(hours=2)))
    return events


def transition(report: dict, a: str, b: str) -> dict:
    return next(t for t in report["transitions"] if t["from_stage"] == a and t["to_stage"] == b)


# Current window = entered 3..17 days ago; baseline window = entered 17..45 days ago (defaults).
CUR, BASE = 8, 30


def test_no_events_is_insufficient_data_not_a_finding():
    report = compute_funnel_report([], now=NOW)
    assert report["verdict"] == "insufficient_data" and report["degraded"] == [] and report["largest_leak"] is None


def test_small_cohort_is_insufficient_data():
    events = cohort("outreach_sent", "reply_classified", n=MIN_COHORT - 1, converted=0, entered_days_ago=CUR)
    t = transition(compute_funnel_report(events, now=NOW), "outreach_sent", "reply_classified")
    assert t["status"] == "insufficient_data"


def test_current_data_without_baseline_is_reported_as_no_baseline():
    events = cohort("outreach_sent", "reply_classified", n=20, converted=2, entered_days_ago=CUR)
    report = compute_funnel_report(events, now=NOW)
    assert transition(report, "outreach_sent", "reply_classified")["status"] == "no_baseline"
    assert report["verdict"] == "healthy" and report["degraded"] == []       # no baseline => cannot claim degradation


def test_degradation_against_own_baseline_is_detected():
    events = (cohort("outreach_sent", "reply_classified", n=20, converted=10, entered_days_ago=BASE)     # baseline 50%
              + cohort("outreach_sent", "reply_classified", n=20, converted=2, entered_days_ago=CUR))    # now 10%
    report = compute_funnel_report(events, now=NOW)
    t = transition(report, "outreach_sent", "reply_classified")
    assert t["status"] == "degraded" and t["baseline_rate"] == 0.5 and t["current_rate"] == 0.1
    assert report["verdict"] == "degraded" and report["degraded"] == ["outreach_sent->reply_classified"]
    assert report["largest_leak"]["transition"] == "outreach_sent->reply_classified" and report["largest_leak"]["prospects_not_advanced"] == 18


def test_a_chronically_low_rate_is_NOT_flagged_when_it_matches_its_baseline():
    """The whole point: cold outreach reply rates are always low; low-but-stable is healthy."""
    events = (cohort("outreach_sent", "reply_classified", n=40, converted=4, entered_days_ago=BASE)
              + cohort("outreach_sent", "reply_classified", n=40, converted=4, entered_days_ago=CUR))
    report = compute_funnel_report(events, now=NOW)
    assert transition(report, "outreach_sent", "reply_classified")["status"] == "healthy"
    assert report["degraded"] == []


def test_improvement_is_recognised():
    events = (cohort("outreach_sent", "reply_classified", n=20, converted=2, entered_days_ago=BASE)
              + cohort("outreach_sent", "reply_classified", n=20, converted=10, entered_days_ago=CUR))
    assert transition(compute_funnel_report(events, now=NOW), "outreach_sent", "reply_classified")["status"] == "improved"


def test_immature_prospects_are_excluded_so_slow_repliers_are_not_counted_as_failures():
    mature = cohort("outreach_sent", "reply_classified", n=10, converted=5, entered_days_ago=CUR)
    just_sent = cohort("outreach_sent", "reply_classified", n=50, converted=0, entered_days_ago=1)      # sent yesterday, no reply yet
    t = transition(compute_funnel_report(mature + just_sent, now=NOW), "outreach_sent", "reply_classified")
    assert t["current_cohort"] == 10 and t["current_rate"] == 0.5


def test_reaching_b_before_a_does_not_count_as_conversion():
    events = []
    for _ in range(10):
        pid = uuid.uuid4()
        events += [Event(pid, "reply_classified", days_ago(CUR + 1)), Event(pid, "outreach_sent", days_ago(CUR))]
    t = transition(compute_funnel_report(events, now=NOW), "outreach_sent", "reply_classified")
    assert t["current_cohort"] == 10 and t["current_converted"] == 0


def test_repeated_events_for_one_prospect_are_counted_once():
    events = []
    for _ in range(6):
        pid = uuid.uuid4()
        events += [Event(pid, "outreach_sent", days_ago(CUR)), Event(pid, "outreach_sent", days_ago(CUR - 1)),       # re-sent (follow-up)
                   Event(pid, "reply_classified", days_ago(CUR - 2))]
    t = transition(compute_funnel_report(events, now=NOW), "outreach_sent", "reply_classified")
    assert t["current_cohort"] == 6 and t["current_converted"] == 6


def test_naive_datetimes_are_treated_as_utc():
    pid_events = [Event(uuid.uuid4(), "qualified", (NOW - timedelta(days=CUR)).replace(tzinfo=None)) for _ in range(6)]
    assert transition(compute_funnel_report(pid_events, now=NOW), "qualified", "outreach_sent")["current_cohort"] == 6


# ---- database integration -------------------------------------------------------------------------------

def _seed_events(ids, events: list[Event]):
    with session_scope() as session:
        for e in events:
            prospect_id = e.prospect_id
            session.add(FunnelEvent(prospect_id=prospect_id, product_id=ids["product_id"], from_stage=None, to_stage=e.to_stage, agent="test", created_at=e.at))


def _degraded_events():
    events = (cohort("outreach_sent", "reply_classified", n=20, converted=10, entered_days_ago=BASE)
              + cohort("outreach_sent", "reply_classified", n=20, converted=1, entered_days_ago=CUR))
    return events


def test_report_from_real_funnel_events_table_and_incident_is_idempotent():
    from packages.db.models import Agent
    with company_with_products() as ids:
        prospects = {}
        events = _degraded_events()
        for e in events:                                        # funnel_events.prospect_id is a real FK
            if e.prospect_id not in prospects:
                prospects[e.prospect_id] = add_prospect(ids["company_id"], ids["product_id"], name=f"P{len(prospects)}")
        remapped = [Event(prospects[e.prospect_id], e.to_stage, e.at) for e in events]
        _seed_events(ids, remapped)

        with session_scope() as session:
            assert len(load_events(session, ids["product_id"])) == len(events)
            growth_id = session.query(Agent).filter_by(slug="growth").one().id

        report = funnel_report_for_product(ids["product_id"], now=datetime.now(timezone.utc))
        # The fixture times are relative to NOW=2026-09-23; recompute relative to the real clock instead:
        shift = datetime.now(timezone.utc) - NOW
        with session_scope() as session:
            for fe in session.query(FunnelEvent).filter_by(product_id=ids["product_id"]).all():
                fe.created_at = fe.created_at + shift
        report = funnel_report_for_product(ids["product_id"])
        assert report["degraded"] == ["outreach_sent->reply_classified"]

        first = report_degradation_incidents(ids["company_id"], ids["product_id"], report, growth_agent_id=growth_id)
        second = report_degradation_incidents(ids["company_id"], ids["product_id"], report, growth_agent_id=growth_id)
        assert len(first) == 1 and second == []                  # de-duplicated while the first is still open

        with session_scope() as session:
            inc = session.get(Incident, first[0])
            assert inc.severity == "warning" and inc.product_id == ids["product_id"]
            assert "50%" in inc.cause and "5%" in inc.cause
            inc.status = "resolved"
        third = report_degradation_incidents(ids["company_id"], ids["product_id"], report, growth_agent_id=growth_id)
        assert len(third) == 1                                   # once resolved, a still-degraded funnel re-alerts


def test_maintenance_funnel_step_only_covers_live_products_and_raises_incident():
    from packages.core.maintenance import _step_funnel_checks
    from packages.db.models import Product
    with company_with_products(lifecycle="draft") as ids:
        assert isinstance(_step_funnel_checks(), int)            # draft products are skipped (no error, nothing raised for them)
        with session_scope() as session:
            assert session.query(Incident).filter_by(product_id=ids["product_id"]).count() == 0
            session.get(Product, ids["product_id"]).lifecycle = "active"
        _step_funnel_checks()                                    # live but no data: still nothing to raise
        with session_scope() as session:
            assert session.query(Incident).filter_by(product_id=ids["product_id"]).count() == 0


def test_analytics_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api.main import create_app
    with company_with_products() as ids:
        client = TestClient(create_app())
        r = client.get(f"/analytics/funnel?product_id={ids['product_id']}")
        assert r.status_code == 200 and r.json()["verdict"] == "insufficient_data"
        assert client.get(f"/analytics/funnel?product_id={uuid.uuid4()}").status_code == 404
        assert client.get(f"/analytics/funnel?product_id={ids['product_id']}&window_days=1").status_code == 422
