"""Dry run (logged, never delivered) must leave NO trace that could make the system treat a prospect as contacted."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from packages.core import autopilot, outbound
from packages.core.maintenance import _step_repair_dry_run_state
from packages.db.models import Approval, FunnelEvent, OutboundMessage, Prospect, Task
from packages.db.session import session_scope
from tests.acq_helpers import Clock, FakeEnrichment, RecordingProvider, acquisition_product, drain, lead, make_registry
from tests.test_acquisition_workflow import World, build_world
import pytest


@pytest.fixture()
def clock(monkeypatch):
    c = Clock(); monkeypatch.setattr(outbound, "_now", c); monkeypatch.setattr(autopilot, "_now", c)
    return c


def stage_of(w, company):
    with session_scope() as s:
        return s.query(Prospect).filter_by(product_id=w.pid, company_name=company).one().stage


def test_a_dry_run_send_does_not_advance_the_prospect_or_record_a_contact(clock):
    with acquisition_product() as ids:
        provider = RecordingProvider(sent=False)                       # dry run: the provider only logs
        w = build_world(ids, clock, [lead(1)], provider=provider)
        w.pump(5); w.approve_all(); w.send_everything_approved()
        assert stage_of(w, "Acme Plumbing 1") == "qualified"          # NOT outreach_sent
        with session_scope() as s:
            assert s.query(FunnelEvent).filter_by(product_id=w.pid, to_stage="outreach_sent").count() == 0
            p = s.query(Prospect).filter_by(product_id=w.pid).one()
            assert p.qualification["dry_run"]["kind"] == "first_contact"
            assert [m.status for m in s.query(OutboundMessage).filter_by(company_id=w.cid).all()] == ["logged"]
        assert outbound.real_first_contacts(w.cid, ["pat1@acme-plumbing-1.com"]) == set()


def test_dry_run_prospects_never_get_follow_ups_even_after_days(clock):
    with acquisition_product(config={"follow_up_after_days": 3}) as ids:
        w = build_world(ids, clock, [lead(1), lead(2)], provider=RecordingProvider(sent=False))
        w.pump(5); w.approve_all(); w.send_everything_approved()
        clock.advance(days=10)
        w.pump(4)
        assert w.pending_approvals() == []                              # no follow-up draft for emails that were never sent
        with session_scope() as s:
            assert s.query(Task).filter(Task.product_id == w.pid, Task.context_refs["follow_up"].astext == "true").count() == 0


def test_follow_up_path_requires_a_real_first_contact_even_if_the_stage_is_wrong(clock):
    """Defence in depth: even a prospect FORCED to outreach_sent with only a logged send is refused a follow-up."""
    with acquisition_product(config={"follow_up_after_days": 3}) as ids:
        w = build_world(ids, clock, [lead(1)], provider=RecordingProvider(sent=False))
        w.pump(5); w.approve_all(); w.send_everything_approved()
        with session_scope() as s:
            p = s.query(Prospect).filter_by(product_id=w.pid).one()
            p.stage = "outreach_sent"                                   # simulate state corrupted by an older version
            s.add(FunnelEvent(prospect_id=p.id, product_id=w.pid, from_stage="qualified", to_stage="outreach_sent", agent="sales"))
        before = len(w.provider.messages)                               # the one dry-run attempt (provider reported sent=False)
        clock.advance(days=5); w.pump(3)
        assert w.pending_approvals() == [] and len(w.provider.messages) == before     # no follow-up was even attempted
        assert [r[2] for r in w.ledger()] == ["logged"]


def test_planner_does_not_redraft_a_rehearsed_prospect_in_dry_run_but_does_once_live(clock, monkeypatch):
    from packages.core.config import get_settings
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)], provider=RecordingProvider(sent=False))
        w.pump(5); w.approve_all(); w.send_everything_approved()
        clock.advance(days=3)                                          # past the 2-day re-draft cooldown
        actions = w.tick()
        assert actions["draft_tasks"] == 0                              # dry run: rehearsed prospects are left alone
        monkeypatch.setenv("OUTREACH_MODE", "smtp"); get_settings.cache_clear()
        try:
            w.provider.sent_flag = True
            w.registry = make_registry(w.enrichment, w.research, w.provider)
            actions = w.tick()
            assert actions["draft_tasks"] == 1                          # going live: the SAME prospect is now eligible for a real first contact
            w.run(); w.approve_all(); w.send_everything_approved()
            assert [(r[0], r[2]) for r in w.ledger() if r[2] == "sent"] == [("pat1@acme-plumbing-1.com", "sent")]
            assert stage_of(w, "Acme Plumbing 1") == "outreach_sent"
        finally:
            get_settings.cache_clear()


def test_a_real_send_after_a_rehearsal_advances_normally_and_counts(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)], provider=RecordingProvider(sent=True))
        w.pump(5); w.approve_all(); w.send_everything_approved()
        assert stage_of(w, "Acme Plumbing 1") == "outreach_sent"
        assert outbound.real_first_contacts(w.cid, ["PAT1@acme-plumbing-1.com"]) == {"pat1@acme-plumbing-1.com"}


def test_maintenance_repairs_prospects_stranded_by_the_old_behaviour(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1), lead(2)], provider=RecordingProvider(sent=True))
        w.pump(5); w.approve_all(); w.send_everything_approved()      # both genuinely sent
        with session_scope() as s:
            a, b = s.query(Prospect).filter_by(product_id=w.pid).order_by(Prospect.company_name).all()
            # stranded: stage advanced although the ledger only ever logged
            s.query(OutboundMessage).filter_by(company_id=w.cid, to_email=a.contact_email).update({"status": "logged"})
            a_id, b_id = a.id, b.id
        assert _step_repair_dry_run_state() >= 1
        with session_scope() as s:
            assert s.get(Prospect, a_id).stage == "qualified" and s.get(Prospect, b_id).stage == "outreach_sent"   # only the stranded one moved
        assert _step_repair_dry_run_state() == 0                       # idempotent
