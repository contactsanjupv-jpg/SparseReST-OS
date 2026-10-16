"""Regressions for the defects found by the real-integration verification: milestone validation, transient DNS, mid-day
reports, the hot-lead command text, and robotic template copy."""
from __future__ import annotations

import ssl
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from packages.core import autopilot, campaign, copywriter as cw, outbound, qualification as q, reporting
from packages.core.domains import StaticDomainChecker
from packages.db.models import FunnelEvent, Incident, OutboundMessage, Product, Prospect, Task
from packages.db.session import session_scope
from scripts import autopilot_ctl
from tests.acq_helpers import Clock, FakeEnrichment, FakeResearch, POSITIONING, RecordingProvider, SALES, acquisition_product, drain, lead, make_registry
from tests.helpers import add_prospect
from tests.test_acquisition_workflow import World, build_world


@pytest.fixture()
def clock(monkeypatch):
    c = Clock(); monkeypatch.setattr(outbound, "_now", c); monkeypatch.setattr(autopilot, "_now", c)
    return c


def slug(ids):
    with session_scope() as s:
        return s.get(Product, ids["product_id"]).slug


def cli(ids, *args):
    out, err = [], []
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        code = autopilot_ctl.main(["--product", slug(ids), *args], out=out.append)
    return code, "\n".join(out) + buf.getvalue()


def ledger(ids, pid, email, status="sent"):
    with session_scope() as s:
        s.add(OutboundMessage(id=uuid.uuid4(), company_id=ids["company_id"], product_id=ids["product_id"], prospect_id=pid, kind="first_contact", to_email=email,
                              to_domain=email.split("@")[1], subject="s", status=status, approval="founder", reserved_at=datetime.now(timezone.utc), sent_at=datetime.now(timezone.utc)))


# ---- customer-state validation -------------------------------------------------------------------------------------------
def test_a_prospect_the_system_never_emailed_cannot_become_a_meeting_or_customer():
    with acquisition_product() as ids:
        pid = add_prospect(ids["company_id"], ids["product_id"], name="Never", email="never@acme-n.com", stage="icp_match")
        for cmd in ("meeting", "customer"):
            code, text = cli(ids, cmd, "never@acme-n.com")
            assert code == 1 and "never actually emailed" in text and "external" in text
        with session_scope() as s:
            assert s.get(Prospect, pid).stage == "icp_match" and s.query(FunnelEvent).filter_by(prospect_id=pid).count() == 0


def test_dry_run_only_contact_does_not_count_as_a_real_contact_for_milestones():
    with acquisition_product() as ids:
        pid = add_prospect(ids["company_id"], ids["product_id"], name="Dry", email="dry@acme-d.com", stage="outreach_sent")
        ledger(ids, pid, "dry@acme-d.com", status="logged")
        assert cli(ids, "meeting", "dry@acme-d.com")[0] == 1


def test_the_real_path_meeting_then_customer_and_stage_order_is_enforced():
    with acquisition_product() as ids:
        pid = add_prospect(ids["company_id"], ids["product_id"], name="Real", email="real@acme-r.com", stage="outreach_sent")
        ledger(ids, pid, "real@acme-r.com")
        code, text = cli(ids, "customer", "real@acme-r.com")                 # skipping the meeting
        assert code == 1 and "record the meeting first" in text
        assert cli(ids, "meeting", "real@acme-r.com")[0] == 0
        code, text = cli(ids, "meeting", "real@acme-r.com"); assert code == 1 and "already recorded" in text
        assert cli(ids, "customer", "real@acme-r.com")[0] == 0
        code, text = cli(ids, "customer", "real@acme-r.com"); assert code == 1 and "already recorded" in text
        p = campaign.progress(ids["product_id"])
        assert p["customers"] == 1 and p["customers_from_outreach"] == 1


def test_a_positive_reply_alone_can_close_but_a_negative_one_cannot():
    with acquisition_product() as ids:
        yes = add_prospect(ids["company_id"], ids["product_id"], name="Yes", email="yes@acme-y.com", stage="reply_classified")
        no = add_prospect(ids["company_id"], ids["product_id"], name="No", email="no@acme-n2.com", stage="reply_classified")
        ledger(ids, yes, "yes@acme-y.com"); ledger(ids, no, "no@acme-n2.com")
        with session_scope() as s:
            s.get(Prospect, yes).reply_classification = "high_intent"; s.get(Prospect, no).reply_classification = "not_interested"
        assert cli(ids, "customer", "yes@acme-y.com")[0] == 0
        assert cli(ids, "customer", "no@acme-n2.com")[0] == 1


def test_external_deals_are_allowed_counted_toward_the_goal_but_reported_separately():
    with acquisition_product(goal=(3, 30)) as ids:
        pid = add_prospect(ids["company_id"], ids["product_id"], name="Referral", email="ref@acme-x.com", stage="icp_match")
        code, text = cli(ids, "customer", "ref@acme-x.com", "--external")
        assert code == 0 and "external" in text
        with session_scope() as s:
            assert s.query(FunnelEvent).filter_by(prospect_id=pid, to_stage="customer").one().agent == "founder_external"
        p = campaign.progress(ids["product_id"])
        assert p["customers"] == 1 and p["customers_from_outreach"] == 0 and p["remaining"] == 2


def test_unknown_email_is_a_clear_error():
    with acquisition_product() as ids:
        code, text = cli(ids, "customer", "ghost@nowhere.com")
        assert code == 1 and "no prospect" in text


# ---- transient DNS ---------------------------------------------------------------------------------------------------------------
GOOD = "acme-plumbing-1.com"


def policy():
    return q.QualificationPolicy.from_icp({"buyer_titles": ["Owner"], "qualification": {"min_score": 0.5}})


def test_unverifiable_dns_is_pending_not_failed_but_a_real_nxdomain_is_a_verdict():
    cand = q.Candidate("pat@acme.com", "https://acme.com", "Owner", "Acme")
    unknown = q.finalize("ingest", q.evaluate_ingest(cand, policy(), StaticDomainChecker({"acme.com": "unknown"})), policy(), require_score=False)
    assert not unknown.passed and unknown.only_retryable_failures and unknown.to_json()["pending_retry"] is True
    invalid = q.finalize("ingest", q.evaluate_ingest(cand, policy(), StaticDomainChecker({"acme.com": "invalid"})), policy(), require_score=False)
    assert not invalid.passed and not invalid.only_retryable_failures
    mixed = q.finalize("ingest", q.evaluate_ingest(q.Candidate("noreply@acme.com", "https://acme.com", "Owner", "Acme"), policy(), StaticDomainChecker({"acme.com": "unknown"})), policy(), require_score=False)
    assert not mixed.only_retryable_failures                                      # a genuine failure alongside the blip is still a failure


def stage_and_q(w, name):
    with session_scope() as s:
        p = s.query(Prospect).filter_by(product_id=w.pid, company_name=name).one()
        return p.stage, dict(p.qualification or {})


def test_a_dns_blip_parks_the_prospect_then_it_qualifies_when_dns_recovers(clock):
    checker = StaticDomainChecker({GOOD: "unknown"})
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)])
        w.registry = make_registry(w.enrichment, w.research, w.provider, checker=checker)
        w.step()
        stage, qual = stage_and_q(w, "Acme Plumbing 1")
        assert stage == "enriched" and qual["pending_retry"] is True                  # parked, NOT disqualified
        w.step()                                                                      # research step re-verifies: still unknown -> deferred
        stage, qual = stage_and_q(w, "Acme Plumbing 1")
        assert stage == "enriched" and qual["dns_retries"] == 1
        checker._mx[GOOD] = "valid"                                                   # DNS recovers
        clock.advance(hours=2)
        w.pump(3)
        assert stage_and_q(w, "Acme Plumbing 1")[0] == "qualified"
        assert len(w.pending_approvals()) == 1


def test_persistent_dns_failure_is_eventually_a_clear_disqualification(clock):
    checker = StaticDomainChecker({GOOD: "unknown"})
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)])
        w.registry = make_registry(w.enrichment, w.research, w.provider, checker=checker)
        for _ in range(8):
            w.step(); clock.advance(hours=2)
        stage, qual = stage_and_q(w, "Acme Plumbing 1")
        assert stage == "icp_match" and qual["failed"] == ["email_domain_mx"] and qual["dns_unavailable_after_retries"] is True and qual["passed"] is False


def test_a_definitive_dns_failure_is_not_retried(clock):
    checker = StaticDomainChecker({GOOD: "invalid"})
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)])
        w.registry = make_registry(w.enrichment, w.research, w.provider, checker=checker)
        w.step()
        assert stage_and_q(w, "Acme Plumbing 1")[0] == "icp_match"


def test_live_sending_is_blocked_until_spf_can_actually_be_verified(monkeypatch):
    from packages.core.config import get_settings
    for k, v in {"OUTREACH_MODE": "smtp", "SMTP_HOST": "h", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p", "SMTP_FROM_EMAIL": "a@xflow.example", "IMAP_HOST": "i"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        class Down(StaticDomainChecker):
            def txt(self, name): return None                                          # DNS lookup FAILED
        with acquisition_product(enable=False) as ids:
            rep = autopilot.readiness(ids["company_id"], ids["product_id"], checker=Down())
            spf = next(c for c in rep["checks"] if c["name"] == "sender_spf")
            assert spf["blocking"] and "could not verify SPF" in spf["detail"] and not rep["ready"]
            ok = StaticDomainChecker(txt={"xflow.example": ["v=spf1 -all"]})
            assert autopilot.readiness(ids["company_id"], ids["product_id"], checker=ok)["ready"]
    finally:
        get_settings.cache_clear()


# ---- mid-day report -----------------------------------------------------------------------------------------------------------------
def test_todays_report_shows_todays_activity_and_does_not_cry_no_prospects(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1), lead(2)])
        w.pump(5)
        code, text = cli(ids, "report")                                              # default: today so far
        assert code == 0 and "today report" in text and "PARTIAL" in text
        assert "Prospects found: 2" in text and "Qualified: 2" in text
        assert "No new prospects were found" not in text
        code, text = cli(ids, "report", "--period", "daily")                         # the previous full day: nothing yet, and that is complete
        assert "Prospects found: 0" in text and "PARTIAL" not in text


def test_an_empty_partial_period_never_triggers_the_no_discovery_bottleneck():
    base = {"paused": None, "pending_approvals": 0, "qualified": 0, "disqualified_after_research": 0, "failed_ingest_gate": 0, "qualification_pass_rate": None,
            "top_disqualification_reasons": [], "prospects_found": 0, "first_contacts_sent": 0, "reply_rate": None, "bounce_rate": None, "positive_replies": 0,
            "meetings_booked": 0, "failed_tasks": 0, "send_failures": 0, "funnel_baseline": {"degraded": [], "largest_leak": None}, "goal": {}}
    assert not [b for b in reporting.find_bottlenecks({**base, "partial": True}) if b["area"] == "discovery"]
    assert [b for b in reporting.find_bottlenecks({**base, "partial": False}) if b["area"] == "discovery"]


# ---- hot-lead command text ------------------------------------------------------------------------------------------------------------
def test_hot_lead_alert_names_the_real_commands(clock):
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1)])
        w.pump(5); w.approve_all(); w.send_everything_approved()
        w.reply("pat1@acme-plumbing-1.com", "Yes interested, call me", uid="1")
        with session_scope() as s:
            action = s.query(Incident).filter(Incident.dedup_key.like("hot_lead:%")).one().action_required
        assert "autopilot_ctl meeting EMAIL" in action and "autopilot_ctl customer EMAIL" in action and "mark-meeting-booked" not in action


# ---- template copy -----------------------------------------------------------------------------------------------------------------------
P = {"company_name": "Acme Plumbing", "contact_name": "Pat Lee", "contact_title": "Owner", "website": "https://acme.com", "email": "pat@acme.com"}


def build(research):
    return cw.build_draft(prospect=P, research=research, positioning=POSITIONING, sales=SALES, llm=type("L", (), {"complete_json": lambda *a, **k: None})())


def test_template_never_claims_what_a_business_offers_from_scraped_list_items():
    """Regression from the real run: pypi.org's list items were 'Register', 'Deutsch', 'English' (menu + language switcher), which the
    template turned into 'you offer Register, Deutsch and English'. List items are not evidence of services."""
    d = build({"hook": "Family plumbing company serving Testville since 1998.", "services": ["Register", "Deutsch", "English", "Home", "Drain cleaning"]})
    assert "you offer" not in d.body and "Register" not in d.body and "Deutsch" not in d.body
    assert "Family plumbing company serving Testville since 1998." in d.body and '"' not in d.body


def test_the_hook_is_used_without_quote_marks_and_trimmed():
    d = build({"hook": "Family plumbing company serving Testville homeowners since 1998 with honest pricing and fast response times across the region.", "services": []})
    assert "this line stood out:" in d.body and '"' not in d.body.split("\n")[2]
    assert len(d.body.split("\n")[2]) < 200 and d.source == "template" and d.problems == []


# ---- TLS: the providers really hand a verifying context to the SMTP client -----------------------------------------------------------------
def test_smtp_provider_passes_a_verifying_context_to_starttls():
    from packages.tools.outreach import SMTPOutreachProvider
    seen = {}

    class Fake:
        def starttls(self, context=None): seen["ctx"] = context
        def login(self, u, p): pass
        def send_message(self, m): pass
        def quit(self): pass
    provider = SMTPOutreachProvider(host="h", port=587, username="u", password="p", from_email="a@b.co", smtp_client_factory=lambda h, p: Fake())
    provider.send(to_email="x@y.co", subject="s", body="b")
    assert seen["ctx"].verify_mode == ssl.CERT_REQUIRED and seen["ctx"].check_hostname is True
