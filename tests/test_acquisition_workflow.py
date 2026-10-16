"""END-TO-END: discover -> ingest gate -> research -> final gate -> draft -> founder approval -> governed, paced send
-> reply monitoring -> follow-up -> hot-lead escalation. Real worker cycle, real gate, real governor, real database;
only the network edges (discovery source, website fetch, SMTP) are faked."""
from __future__ import annotations

import smtplib
from datetime import timedelta

import pytest

from packages.core import autopilot, outbound
from packages.core.inbound import process_inbound_for_company
from packages.core.services import ApprovalService
from packages.db.models import Approval, AutopilotState, FunnelEvent, Incident, OutboundMessage, Prospect, Suppression, Task
from packages.db.session import session_scope
from packages.tools.imap_inbound import InboundReply
from tests.acq_helpers import (
    Clock, FakeEnrichment, FakeResearch, RecordingProvider, acquisition_product, drain, good_research, lead, make_registry,
)


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(outbound, "_now", c)
    monkeypatch.setattr(autopilot, "_now", c)
    return c


class World:
    def __init__(self, ids, clock, enrichment, research, provider, registry):
        self.ids, self.clock, self.enrichment, self.research, self.provider, self.registry = ids, clock, enrichment, research, provider, registry
        self.cid, self.pid = ids["company_id"], ids["product_id"]

    def tick(self):
        return autopilot.tick(self.cid, self.pid, now=self.clock.now)

    def run(self):
        return drain(self.registry, self.pid)

    def step(self):
        actions = self.tick()
        self.run()
        return actions

    def pump(self, rounds=6):
        for _ in range(rounds):
            self.step()

    def pending_approvals(self):
        with session_scope() as s:
            return [a.id for a in s.query(Approval).join(Task, Task.id == Approval.task_id).filter(
                Approval.status == "pending", Task.product_id == self.pid, Approval.requested_by == "sales").all()]

    def approve_all(self):
        ids = self.pending_approvals()
        for i in ids:
            ApprovalService.decide(i, approved=True)
        return len(ids)

    def send_everything_approved(self, max_rounds=60):
        for _ in range(max_rounds):
            self.run()
            self.clock.advance(minutes=2)
            if not self._open_sends():
                break

    def _open_sends(self):
        with session_scope() as s:
            return s.query(Task).filter(Task.product_id == self.pid, Task.status.in_(("pending", "in_progress")),
                                        Task.context_refs["action"].astext == "draft_and_send").count()

    def stages(self):
        with session_scope() as s:
            return {p.company_name: p.stage for p in s.query(Prospect).filter_by(product_id=self.pid).all()}

    def ledger(self, **filters):
        with session_scope() as s:
            return [(m.to_email, m.kind, m.status, m.approval) for m in s.query(OutboundMessage).filter_by(company_id=self.cid, **filters).order_by(OutboundMessage.reserved_at).all()]

    def reply(self, email, text, subject="Re: Quick question", uid="1", mid=None):
        class Checker:
            def __init__(self, r): self.r, self.marked = [r], []
            def fetch_unread_replies(self, mark_seen=True): return self.r
            def mark_seen(self, uids): self.marked += uids
        result = process_inbound_for_company(self.cid, Checker(InboundReply(from_email=email, subject=subject, body=text, imap_uid=uid, message_id=mid or f"<{uid}-{email}@x>")))
        self.run()
        return result


def build_world(ids, clock, leads, research=None, provider=None, llm=None):
    enrichment = FakeEnrichment(leads)
    research = research or FakeResearch()
    provider = provider or RecordingProvider()
    return World(ids, clock, enrichment, research, provider, make_registry(enrichment, research, provider, llm))


def test_full_pipeline_sends_only_to_prospects_that_pass_every_gate(clock):
    leads = [lead(1), lead(2), lead(3), lead(4),
             lead(5, email="noreply@acme-plumbing-5.com"),        # fails ingest: system address
             lead(6, email="pat6@gmail.com"),                      # fails ingest: free-mail
             lead(7), lead(8)]
    research = FakeResearch(pages={
        "https://acme-plumbing-7.com": {"status": "unreachable", "note": "timeout", "fetched_at": "now"},               # fails research: site down
        "https://acme-plumbing-8.com": {**good_research("x"), "text": "we sell handmade candles and soap " * 20},       # fails research: not on-ICP
    })
    with acquisition_product() as ids:
        w = build_world(ids, clock, leads, research)

        first = w.step()
        assert first["discovery_tasks"] == 1 and first["draft_tasks"] == 0          # nothing qualified yet: nothing to draft, nothing padded
        w.step()
        stages = w.stages()
        assert stages["Acme Plumbing 5"] == "icp_match" and stages["Acme Plumbing 6"] == "icp_match"
        w.step()                                                                   # research tasks run
        stages = w.stages()
        assert [stages[f"Acme Plumbing {i}"] for i in (1, 2, 3, 4)] == ["qualified"] * 4
        assert stages["Acme Plumbing 7"] == "icp_match" and stages["Acme Plumbing 8"] == "icp_match"

        with session_scope() as s:
            q7 = s.query(Prospect).filter_by(product_id=w.pid, company_name="Acme Plumbing 7").one().qualification
            assert "site_reachable" in q7["failed"] and q7["passed"] is False
            q8 = s.query(Prospect).filter_by(product_id=w.pid, company_name="Acme Plumbing 8").one().qualification
            assert "industry_keyword_match" in q8["failed"]

        w.step()                                                                   # drafts for exactly the 4 qualified prospects
        approvals = w.pending_approvals()
        assert len(approvals) == 4 and w.provider.messages == []                   # APPROVAL MODE: nothing leaves without the founder
        with session_scope() as s:
            d = s.get(Approval, approvals[0]).details
            assert d["to"].startswith("pat") and "Acme Plumbing" in d["subject"] and "100 Test Street" in d["body"] and "unsubscribe" in d["body"].lower()
            assert d["hook"] and d["draft_source"] == "template"

        assert w.approve_all() == 4
        w.send_everything_approved()
        sent_to = sorted(m["to"] for m in w.provider.messages)
        assert sent_to == [f"pat{i}@acme-plumbing-{i}.com" for i in (1, 2, 3, 4)]                          # 4 sends, not 30: no quota padding
        assert all(row[2] == "sent" for row in w.ledger())
        assert all(w.stages()[f"Acme Plumbing {i}"] == "outreach_sent" for i in (1, 2, 3, 4))
        times = []
        with session_scope() as s:
            times = sorted(m.reserved_at for m in s.query(OutboundMessage).filter_by(company_id=w.cid).all())
        assert all((b - a).total_seconds() >= 60 for a, b in zip(times, times[1:])), "sends must be paced, not bursty"


def test_hard_gate_never_pads_to_the_daily_cap(clock):
    with acquisition_product(config={"daily_cap": 30}) as ids:
        w = build_world(ids, clock, [lead(1), lead(2), lead(3)])
        w.pump(5)
        assert len(w.pending_approvals()) == 3
        w.approve_all(); w.send_everything_approved()
        assert len(w.provider.messages) == 3
        assert outbound.capacity_snapshot(w.cid, w.pid, clock.now)["remaining"] == 27         # 27 unused slots stay unused


def test_nothing_is_sent_while_autopilot_is_off_and_nothing_is_planned(clock):
    with acquisition_product(enable=False) as ids:
        w = build_world(ids, clock, [lead(1), lead(2)])
        actions = w.tick()
        assert actions["skipped"] == "autopilot is OFF" and actions["discovery_tasks"] == 0
        assert w.provider.messages == [] and w.stages() == {}


def test_approved_send_waits_when_autopilot_is_switched_off_then_proceeds_when_on(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)])
        w.pump(5)
        w.approve_all()
        autopilot.disable(w.cid, w.pid)
        w.run()
        assert w.provider.messages == []                                          # approved, but the switch is OFF: held
        autopilot.enable(w.cid, w.pid)
        w.send_everything_approved()
        assert len(w.provider.messages) == 1


def test_founder_rejection_removes_the_prospect_for_good(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1), lead(2)])
        w.pump(5)
        first = w.pending_approvals()[0]
        ApprovalService.decide(first, approved=False)
        w.clock.advance(days=3)
        w.pump(3)
        assert sum(1 for s in w.stages().values() if s == "icp_match") == 1             # the rejected one is out
        with session_scope() as s:
            assert any("founder_rejected" in (p.qualification or {}).get("failed", []) for p in s.query(Prospect).filter_by(product_id=w.pid).all())
        assert len(w.pending_approvals()) == 1                                         # and it was NOT re-drafted


def test_duplicate_prospects_and_suppressed_contacts_are_never_ingested(clock):
    with acquisition_product() as ids:
        outbound.suppress(ids["company_id"], "email", "pat2@acme-plumbing-2.com", "unsubscribe")
        outbound.suppress(ids["company_id"], "domain", "acme-plumbing-3.com", "manual")
        w = build_world(ids, clock, [lead(1), lead(1), lead(2), lead(3), lead(1, name="Acme Plumbing One (dup email)")])
        w.step()
        assert sorted(w.stages()) == ["Acme Plumbing 1"]                              # only the first Acme 1; the rest are dup/suppressed
        with session_scope() as s:
            assert s.query(Prospect).filter_by(product_id=w.pid).count() == 1


def test_replies_unsubscribe_hot_lead_and_out_of_office(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1), lead(2), lead(3), lead(4)])
        w.pump(5); w.approve_all(); w.send_everything_approved()
        assert len(w.provider.messages) == 4

        r = w.reply("pat1@acme-plumbing-1.com", "Yes, very interested -- can we talk tomorrow? Call me on 555-0100.", uid="1")
        assert len(r.task_ids) == 1
        r = w.reply("pat2@acme-plumbing-2.com", "Please unsubscribe me and do not contact me again.", uid="2")
        w.reply("pat3@acme-plumbing-3.com", "I am out of the office until next Monday with limited access to email.", uid="3")

        stages = w.stages()
        assert stages["Acme Plumbing 1"] == "reply_classified" and stages["Acme Plumbing 2"] == "reply_classified"
        assert stages["Acme Plumbing 3"] == "outreach_sent"                          # OOO is not a reply: stays in the follow-up path
        with session_scope() as s:
            hot = s.query(Incident).filter(Incident.product_id == w.pid, Incident.dedup_key.like("hot_lead:%")).all()
            assert len(hot) == 1 and "Call me on 555-0100" in hot[0].cause and "pat1@acme-plumbing-1.com" in hot[0].action_required
            p2 = s.query(Prospect).filter_by(product_id=w.pid, company_name="Acme Plumbing 2").one()
            assert p2.do_not_contact and p2.reply_classification == "unsubscribe"
            assert s.query(Suppression).filter_by(company_id=w.cid, value="pat2@acme-plumbing-2.com", reason="unsubscribe").count() == 1
        assert outbound.reserve(company_id=w.cid, product_id=w.pid, prospect_id=None, task_id=None, to_email="pat2@acme-plumbing-2.com",
                                kind="follow_up", subject="x", approval="founder", autopilot=False, now=clock.now + timedelta(days=9)).code == "suppressed"


def test_one_follow_up_to_silent_prospects_only(clock):
    with acquisition_product(config={"follow_up_after_days": 3}) as ids:
        w = build_world(ids, clock, [lead(1), lead(2)])
        w.pump(5); w.approve_all(); w.send_everything_approved()
        assert len(w.provider.messages) == 2
        w.reply("pat1@acme-plumbing-1.com", "Not interested, thanks.", uid="9")

        w.clock.advance(days=4)
        # backdate the outreach so the real-clock cutoff in Growth sees it as stale
        w.pump(3)
        approvals = w.pending_approvals()
        assert len(approvals) == 1                                                  # only the silent one; the replier is left alone
        with session_scope() as s:
            assert s.get(Approval, approvals[0]).details["kind"] == "follow_up"
        w.approve_all(); w.send_everything_approved()
        assert len(w.provider.messages) == 3 and w.provider.messages[-1]["to"] == "pat2@acme-plumbing-2.com"
        w.clock.advance(days=10); w.pump(4)
        assert w.pending_approvals() == [] and len(w.provider.messages) == 3         # never a second follow-up


def test_follow_up_is_cancelled_if_the_prospect_replied_in_the_meantime(clock):
    with acquisition_product(config={"follow_up_after_days": 3}) as ids:
        w = build_world(ids, clock, [lead(1)])
        w.pump(5); w.approve_all(); w.send_everything_approved()
        w.clock.advance(days=4); w.pump(2)
        assert len(w.pending_approvals()) == 1
        w.reply("pat1@acme-plumbing-1.com", "Actually yes, let's talk.", uid="5")
        w.approve_all(); w.send_everything_approved()
        assert len(w.provider.messages) == 1                                         # the follow-up never went out
        with session_scope() as s:
            t = s.query(Task).filter(Task.product_id == w.pid, Task.context_refs["follow_up"].astext == "true").one()
            assert t.result["status"] == "blocked" and "replied" in t.result["reason"]


def test_bounce_notice_suppresses_address_and_is_not_treated_as_a_reply(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)])
        w.pump(5); w.approve_all(); w.send_everything_approved()
        dsn = "Final-Recipient: rfc822; pat1@acme-plumbing-1.com\nAction: failed\nStatus: 5.1.1\nDiagnostic-Code: smtp; 550 user unknown"
        res = w.reply("MAILER-DAEMON@mx.example.net", dsn, subject="Delivery Status Notification (Failure)", uid="b1")
        assert res.bounces == 1 and res.task_ids == [] and res.unmatched == 0
        assert w.ledger()[0][2] == "bounced"
        with session_scope() as s:
            assert s.query(Suppression).filter_by(company_id=w.cid, value="pat1@acme-plumbing-1.com", reason="hard_bounce").count() == 1
            assert s.query(Incident).filter(Incident.product_id == w.pid, Incident.title.like("Unmatched%")).count() == 0


def test_recipient_refused_by_server_is_suppressed_not_retried_and_does_not_pause_alone(clock):
    provider = RecordingProvider()
    provider.fail_with, provider.fail_times = smtplib.SMTPRecipientsRefused({"pat1@acme-plumbing-1.com": (550, b"no such user")}), 1
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1), lead(2)], provider=provider)
        w.pump(5); w.approve_all(); w.send_everything_approved()
        assert [m["to"] for m in provider.messages] == ["pat2@acme-plumbing-2.com"]
        rows = {r[0]: r[2] for r in w.ledger()}
        assert rows["pat1@acme-plumbing-1.com"] == "failed" and rows["pat2@acme-plumbing-2.com"] == "sent"
        with session_scope() as s:
            assert s.query(Suppression).filter_by(company_id=w.cid, value="pat1@acme-plumbing-1.com").count() == 1
            assert s.get(AutopilotState, w.pid).paused_reason is None
            assert s.query(Task).filter(Task.product_id == w.pid, Task.status == "failed", Task.context_refs["action"].astext == "draft_and_send").count() == 0


def test_repeated_smtp_auth_failures_retry_then_pause_sending_and_alert_once(clock):
    provider = RecordingProvider()
    provider.fail_with, provider.fail_times = smtplib.SMTPAuthenticationError(535, b"bad credentials"), 99
    with acquisition_product(config={"consecutive_failure_pause": 3}) as ids:
        w = build_world(ids, clock, [lead(1), lead(2), lead(3), lead(4)], provider=provider)
        w.pump(5); w.approve_all()
        for _ in range(40):
            w.run(); w.clock.advance(minutes=20)
        assert provider.messages == []
        with session_scope() as s:
            state = s.get(AutopilotState, w.pid)
            assert state.paused_reason and "failed on our side" in state.paused_reason
            assert s.query(Incident).filter_by(product_id=w.pid, dedup_key="safety_pause", status="open").count() == 1
        provider.fail_times = 0
        outbound.resume(w.pid)
        w.send_everything_approved()
        assert len(provider.messages) >= 1                                           # after the founder fixes it and resumes, work continues


def test_paused_breaker_stops_planning_and_sending(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)])
        outbound.trip_breaker(w.cid, w.pid, "manual test pause")
        actions = w.tick()
        assert "paused" in actions["skipped"] and actions["discovery_tasks"] == 0 and w.stages() == {}


def test_unapproved_drafts_expire_and_can_be_redrafted(clock):
    with acquisition_product(config={"max_task_age_days": 2}) as ids:
        w = build_world(ids, clock, [lead(1)])
        w.pump(5)
        assert len(w.pending_approvals()) == 1
        clock.advance(days=3)                                                       # the draft has now waited 3 days
        actions = w.tick()
        assert actions["expired"] == 1 and w.pending_approvals() == []
        with session_scope() as s:
            assert s.query(Approval).join(Task, Task.id == Approval.task_id).filter(Task.product_id == w.pid, Approval.status == "expired").count() == 1
            assert s.query(Task).filter_by(product_id=w.pid, status="cancelled").count() == 1


def test_discovery_rotates_geography_then_reports_exhaustion_without_lowering_standards(clock):
    icp = {"industries": ["plumbing"], "geography": "Alpha", "geographies": ["Beta", "Gamma"], "buyer_titles": ["Owner"],
           "qualification": {"min_score": 0.5, "required_keywords_any": ["plumb"]}}
    with acquisition_product(icp=icp) as ids:
        w = build_world(ids, clock, [])
        for _ in range(12):
            w.step()
        calls = [c["icp"]["geography"] for c in w.enrichment.calls]
        assert calls[:2] == ["Alpha", "Alpha"] and "Beta" in calls and "Gamma" in calls          # rotated through every location
        with session_scope() as s:
            inc = s.query(Incident).filter_by(product_id=w.pid, dedup_key="discovery_exhausted").all()
            assert len(inc) == 1 and "NOT lower" in inc[0].impact
            assert s.get(AutopilotState, w.pid).cursor["exhausted"] is True
        assert w.stages() == {}


def test_hot_lead_unanswered_past_sla_is_escalated_once(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)])
        w.pump(5); w.approve_all(); w.send_everything_approved()
        w.reply("pat1@acme-plumbing-1.com", "Yes please call me, I want to buy.", uid="h1")
        clock.advance(hours=6)                                                      # unanswered for 6h > the 4h SLA
        assert w.tick()["escalated"] == 1 and w.tick()["escalated"] == 0
        with session_scope() as s:
            inc = s.query(Incident).filter(Incident.dedup_key.like("hot_lead:%")).one()
            assert inc.severity == "critical" and inc.escalated_at is not None


def test_pending_approval_reminder_is_one_deduplicated_incident_that_clears_itself(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1), lead(2)])
        w.pump(5)
        clock.advance(hours=20)
        w.tick(); w.tick()
        with session_scope() as s:
            assert s.query(Incident).filter_by(product_id=w.pid, dedup_key="approvals_waiting", status="open").count() == 1
        w.approve_all()
        w.tick()
        with session_scope() as s:
            assert s.query(Incident).filter_by(product_id=w.pid, dedup_key="approvals_waiting", status="open").count() == 0


def test_stale_task_id_cannot_contact_a_prospect_twice(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)])
        w.pump(5); w.approve_all(); w.send_everything_approved()
        pid = next(iter(w.stages()))
        with session_scope() as s:
            prospect_id = s.query(Prospect).filter_by(product_id=w.pid).one().id
        from packages.core.context import Context
        from packages.core.services import TaskService
        from packages.db.models import Agent
        with session_scope() as s:
            sales_id = s.query(Agent).filter_by(slug="sales").one().id
        ctx = Context(company_id=w.cid, agent_id=sales_id, product_id=w.pid)
        TaskService.create(ctx, goal="replayed", created_by="test", context_refs={"action": "draft_and_send", "prospect_id": str(prospect_id), "approved": True, "origin": "autopilot"})
        w.run()
        assert len(w.provider.messages) == 1
