"""Daily/weekly growth reports: real numbers from real rows, honest bottlenecks, idempotent, delivered."""
from __future__ import annotations

import smtplib
from datetime import timedelta

import pytest

from packages.core import autopilot, outbound, reporting
from packages.core.config import get_settings
from packages.db.models import FunnelEvent, GrowthReport, Incident, Prospect
from packages.db.session import session_scope
from tests.acq_helpers import Clock, FakeEnrichment, FakeResearch, RecordingProvider, acquisition_product, drain, good_research, lead, make_registry
from tests.test_acquisition_workflow import World, build_world


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(outbound, "_now", c); monkeypatch.setattr(autopilot, "_now", c)
    return c


def window(clock):
    now = clock.now
    return now - timedelta(days=1), now + timedelta(days=1)


def populated(ids, clock):
    leads = [lead(i) for i in range(1, 7)] + [lead(7, email="noreply@acme-plumbing-7.com")]
    research = FakeResearch(pages={"https://acme-plumbing-6.com": {"status": "unreachable", "note": "t", "fetched_at": "n"}})
    w = build_world(ids, clock, leads, research)
    w.pump(5); w.approve_all(); w.send_everything_approved()
    w.reply("pat1@acme-plumbing-1.com", "Yes interested, call me tomorrow please", uid="1")
    w.reply("pat2@acme-plumbing-2.com", "Not interested", uid="2")
    return w


def test_metrics_reflect_the_real_pipeline(clock):
    with acquisition_product() as ids:
        w = populated(ids, clock)
        m = reporting.compute_metrics(w.cid, w.pid, *window(clock))
        assert m["prospects_found"] == 7 and m["failed_ingest_gate"] == 1 and m["disqualified_after_research"] == 1 and m["qualified"] == 5
        assert m["qualification_pass_rate"] == pytest.approx(5 / 7, abs=0.001)
        assert m["first_contacts_sent"] == 5 and m["follow_ups_sent"] == 0 and m["dry_run_logged"] == 0
        assert m["replies"] == 2 and m["positive_replies"] == 1 and m["reply_rate"] == pytest.approx(0.4)
        assert m["reply_breakdown"] == {"high_intent": 1, "not_interested": 1}
        assert dict(m["top_disqualification_reasons"]) == {"email_local_part_ok": 1, "site_reachable": 1}
        assert m["cumulative"]["contacted"] == 5 and m["cumulative"]["customers"] == 0 and m["customers_remaining"] == 10
        assert any("High-intent" in n["title"] for n in m["needs_founder"])


def test_stub_sends_are_reported_as_dry_run_not_as_real_outreach(clock):
    with acquisition_product() as ids:
        provider = RecordingProvider(sent=False)                                  # provider that only logs
        w = build_world(ids, clock, [lead(1)], provider=provider)
        w.pump(5); w.approve_all(); w.send_everything_approved()
        m = reporting.compute_metrics(w.cid, w.pid, *window(clock))
        assert m["first_contacts_sent"] == 0 and m["dry_run_logged"] == 1
        assert "DRY RUN" in reporting.render_text("XFlow", "daily", m, [])


def test_meetings_and_customers_are_counted_from_founder_actions(clock):
    with acquisition_product() as ids:
        w = populated(ids, clock)
        with session_scope() as s:
            p = s.query(Prospect).filter_by(product_id=w.pid, company_name="Acme Plumbing 1").one()
            s.add(FunnelEvent(prospect_id=p.id, product_id=w.pid, from_stage="reply_classified", to_stage="meeting_booked", agent="founder"))
            s.add(FunnelEvent(prospect_id=p.id, product_id=w.pid, from_stage="meeting_booked", to_stage="customer", agent="founder"))
        m = reporting.compute_metrics(w.cid, w.pid, *window(clock))
        assert m["meetings_booked"] == 1 and m["customers"] == 1 and m["cumulative"]["customers"] == 1 and m["customers_remaining"] == 9
        assert "1 / 10 customers" in reporting.render_text("XFlow", "daily", m, [])


def test_bottleneck_rules_fire_only_with_enough_data_and_never_blame_small_samples(clock):
    base = {"paused": None, "pending_approvals": 0, "qualified": 0, "disqualified_after_research": 0, "failed_ingest_gate": 0, "qualification_pass_rate": None,
            "top_disqualification_reasons": [], "prospects_found": 5, "first_contacts_sent": 0, "reply_rate": None, "bounce_rate": None, "positive_replies": 0,
            "meetings_booked": 0, "failed_tasks": 0, "send_failures": 0, "funnel_baseline": {"degraded": [], "largest_leak": None}}
    assert reporting.find_bottlenecks(base) == []
    assert any(b["area"] == "qualification" for b in reporting.find_bottlenecks({**base, "qualified": 2, "disqualified_after_research": 30, "failed_ingest_gate": 10,
                                                                                  "qualification_pass_rate": 0.05, "top_disqualification_reasons": [("site_reachable", 30)]}))
    assert not any(b["area"] == "qualification" for b in reporting.find_bottlenecks({**base, "qualified": 0, "disqualified_after_research": 4, "qualification_pass_rate": 0.0}))
    assert any(b["area"] == "messaging" for b in reporting.find_bottlenecks({**base, "first_contacts_sent": 40, "reply_rate": 0.0}))
    assert not any(b["area"] == "messaging" for b in reporting.find_bottlenecks({**base, "first_contacts_sent": 5, "reply_rate": 0.0}))
    assert any(b["area"] == "conversion" for b in reporting.find_bottlenecks({**base, "positive_replies": 2}))
    assert reporting.find_bottlenecks({**base, "paused": "bounces"})[0]["severity"] == "critical"
    assert any(b["area"] == "list_quality" for b in reporting.find_bottlenecks({**base, "first_contacts_sent": 20, "bounce_rate": 0.1}))


def test_generate_is_idempotent_and_stores_text_metrics_and_bottlenecks(clock):
    with acquisition_product() as ids:
        w = populated(ids, clock)
        start, end = window(clock)
        rid = reporting.generate(w.cid, w.pid, "daily", start, end)
        assert rid is not None and reporting.generate(w.cid, w.pid, "daily", start, end) is None
        with session_scope() as s:
            r = s.get(GrowthReport, rid)
            assert r.metrics["first_contacts_sent"] == 5 and "NEEDS YOU" in r.summary and "GOAL: 0 / 10 customers" in r.summary
            assert isinstance(r.bottlenecks, list)


def test_delivery_email_slack_and_console_fallback(clock, monkeypatch, capsys):
    with acquisition_product() as ids:
        w = populated(ids, clock)
        rid = reporting.generate(w.cid, w.pid, "daily", *window(clock))
        assert reporting.deliver(rid) == ["console"] and "acquisition" in capsys.readouterr().out          # $0 fallback

        sent = []

        class FakeSMTP:
            def __init__(self, host, port): self.host = host
            def starttls(self, context=None): pass
            def login(self, u, p): pass
            def send_message(self, msg): sent.append((msg["To"], msg["Subject"], msg.get_content()))
            def quit(self): pass
        posts = []
        for k, v in {"SMTP_HOST": "h", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p", "SMTP_FROM_EMAIL": "a@x.com", "REPORT_EMAIL_TO": "founder@x.com",
                     "SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"}.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        try:
            channels = reporting.deliver(rid, smtp_factory=lambda h, p: FakeSMTP(h, p), http_post=lambda url, json, timeout: posts.append((url, json)))
            assert channels == ["email", "slack"]
            assert sent[0][0] == "founder@x.com" and "daily growth report" in sent[0][1] and "NEEDS YOU" in sent[0][2]
            assert posts[0][0] == "https://hooks.slack.test/x" and "customers" in posts[0][1]["text"]

            class Broken(FakeSMTP):
                def starttls(self, context=None): raise smtplib.SMTPException("down")
            assert reporting.deliver(rid, smtp_factory=lambda h, p: Broken(h, p), http_post=lambda url, json, timeout: None) == ["slack"]     # a dead SMTP server doesn't lose the report
        finally:
            get_settings.cache_clear()
        with session_scope() as s:
            assert s.get(GrowthReport, rid).delivered_at is not None


def test_maybe_generate_daily_weekly_at_report_hour_and_only_for_active_products(clock):
    from datetime import datetime, timezone
    with acquisition_product(enable=False) as ids:
        c, p = ids["company_id"], ids["product_id"]
        assert reporting.maybe_generate(c, p, deliver_now=False) == []                  # never enabled, nothing sent: no reports
    with acquisition_product(config={"report_hour": 8, "weekly_report_weekday": 0, "timezone": "UTC"}) as ids:
        c, p = ids["company_id"], ids["product_id"]
        tuesday_7 = datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)
        assert reporting.maybe_generate(c, p, tuesday_7, deliver_now=False) == []       # before report hour
        tuesday_9 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
        assert len(reporting.maybe_generate(c, p, tuesday_9, deliver_now=False)) == 1   # daily only (Tuesday)
        assert reporting.maybe_generate(c, p, tuesday_9, deliver_now=False) == []       # idempotent
        monday_9 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
        assert len(reporting.maybe_generate(c, p, monday_9, deliver_now=False)) == 2    # daily + weekly
        with session_scope() as s:
            periods = sorted(r.period for r in s.query(GrowthReport).filter_by(product_id=p).all())
            assert periods == ["daily", "daily", "weekly"]


def test_cli_report_prints_latest_or_computes_on_demand(clock):
    from scripts import autopilot_ctl
    from packages.db.models import Product
    with acquisition_product() as ids:
        w = populated(ids, clock)
        with session_scope() as s:
            slug = s.get(Product, w.pid).slug
        out = []
        assert autopilot_ctl.main(["--product", slug, "report"], out=out.append) == 0
        assert "today report" in out[0] and "GOAL:" in out[0] and "PARTIAL" in out[0]
