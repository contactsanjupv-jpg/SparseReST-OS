"""The outbound governor: caps, warm-up, windows, pacing, suppression, dedup, cooldown, breakers, bounces."""
from __future__ import annotations

import random
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from packages.core import outbound
from packages.core.autopilot_config import AutopilotConfig, ConfigError
from packages.db.models import AutopilotState, Incident, OutboundMessage, Prospect, Suppression, Task
from packages.db.session import session_scope
from tests.acq_helpers import acquisition_product, monday_10am_utc
from tests.helpers import add_prospect

NOW = monday_10am_utc()
RNG = random.Random(1)


def reserve(ids, email, *, kind="first_contact", autopilot=True, now=NOW, prospect=None, task=None):
    return outbound.reserve(company_id=ids["company_id"], product_id=ids["product_id"], prospect_id=prospect or add_prospect(ids["company_id"], ids["product_id"], name=email, email=email),
                            task_id=task, to_email=email, kind=kind, subject="s", approval="founder", autopilot=autopilot, now=now, rng=RNG)


def sent(ids, decision, at=NOW):
    outbound.confirm_sent(decision.message_id, "<m>", at)


def set_cfg(ids, **kw):
    from packages.core import autopilot
    autopilot.update_config(ids["company_id"], ids["product_id"], kw)


def enable_state(ids):
    with session_scope() as s:
        s.get(AutopilotState, ids["product_id"]).enabled = True


# ---- config ------------------------------------------------------------------------------------------
def test_config_validation_rejects_unsafe_values():
    for bad in ({"daily_cap": 0}, {"daily_cap": 31}, {"start_hour": 18, "end_hour": 9}, {"send_days": []}, {"timezone": "Mars/Base"},
                {"min_gap_seconds": 5}, {"jitter": 0.9}, {"bogus_key": 1}):
        with pytest.raises(ConfigError):
            AutopilotConfig.from_dict(bad)


def test_hard_ceiling_cannot_be_exceeded_by_config(monkeypatch):
    from packages.core.config import get_settings
    assert AutopilotConfig.from_dict({"daily_cap": 30}).daily_cap == 30
    with pytest.raises(ConfigError):
        AutopilotConfig.from_dict({"daily_cap": 31})
    monkeypatch.setenv("OUTBOUND_HARD_CEILING", "50"); get_settings.cache_clear()
    try:
        assert AutopilotConfig.from_dict({"daily_cap": 45}).daily_cap == 45
    finally:
        monkeypatch.delenv("OUTBOUND_HARD_CEILING"); get_settings.cache_clear()


def test_warmup_schedule_and_gap():
    cfg = AutopilotConfig.from_dict({"daily_cap": 30, "warmup": True})
    assert cfg.effective_cap(None, NOW) == 10
    assert cfg.effective_cap(NOW - timedelta(days=8), NOW) == 20
    assert cfg.effective_cap(NOW - timedelta(days=20), NOW) == 30
    assert AutopilotConfig.from_dict({"daily_cap": 30, "warmup": False}).effective_cap(None, NOW) == 30
    assert cfg.gap_seconds(30) == pytest.approx(8 * 3600 / 30)                     # 9-17h spread across the day


def test_send_window_in_recipient_timezone():
    cfg = AutopilotConfig.from_dict({"timezone": "America/Chicago", "start_hour": 9, "end_hour": 17})
    assert cfg.in_window(datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc))        # Monday 11:00 Chicago
    assert not cfg.in_window(datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc))    # Monday 07:00 Chicago
    assert not cfg.in_window(datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc))   # Saturday
    nxt = cfg.next_window_open(datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc))  # Friday evening -> Monday 09:00 Chicago
    assert cfg.local(nxt).weekday() == 0 and cfg.local(nxt).hour == 9


# ---- reservation rules ---------------------------------------------------------------------------------
def test_autopilot_off_defers_autopilot_sends_but_not_manual_ones():
    with acquisition_product(enable=False) as ids:
        d = reserve(ids, "a@acme-a.com")
        assert not d.allowed and d.code == "autopilot_off" and d.defer
        assert reserve(ids, "b@acme-b.com", autopilot=False).allowed                   # a founder-initiated manual send still works


def test_daily_cap_is_never_exceeded_and_defers_to_next_window():
    with acquisition_product(config={"daily_cap": 3, "min_gap_seconds": 60}) as ids:
        enable_state(ids)
        t = NOW
        for i in range(3):
            d = reserve(ids, f"p{i}@acme-{i}.com", now=t)
            assert d.allowed, d.reason
            sent(ids, d, t)
            t += timedelta(minutes=5)
        over = reserve(ids, "p9@acme-9.com", now=t)
        assert not over.allowed and over.code == "daily_cap" and over.retry_at > t
        assert outbound.capacity_snapshot(ids["company_id"], ids["product_id"], t)["remaining"] == 0


def test_cap_counts_across_manual_and_autopilot_sends():
    with acquisition_product(config={"daily_cap": 2}) as ids:
        enable_state(ids)
        sent(ids, reserve(ids, "a@acme-a.com", autopilot=False))
        sent(ids, reserve(ids, "b@acme-b.com", autopilot=False, now=NOW + timedelta(minutes=2)))
        assert reserve(ids, "c@acme-c.com", autopilot=False, now=NOW + timedelta(minutes=4)).code == "daily_cap"


def test_dry_run_logged_messages_do_not_consume_real_capacity():
    with acquisition_product(config={"daily_cap": 1}) as ids:
        enable_state(ids)
        d = reserve(ids, "a@acme-a.com")
        outbound.confirm_logged(d.message_id, "stub provider")
        assert reserve(ids, "b@acme-b.com", now=NOW + timedelta(minutes=5)).allowed


def test_pacing_prevents_bursts_and_spaces_deferred_sends_apart():
    with acquisition_product(config={"daily_cap": 30, "min_gap_seconds": 300}) as ids:
        enable_state(ids)
        first = reserve(ids, "a@acme-a.com")
        assert first.allowed
        sent(ids, first)
        second = reserve(ids, "b@acme-b.com", now=NOW + timedelta(seconds=10))
        assert second.code == "pacing" and second.retry_at >= NOW + timedelta(seconds=300)
        later = reserve(ids, "b@acme-b.com", now=NOW + timedelta(seconds=301))
        assert later.allowed


def test_outside_window_defers_to_next_open():
    with acquisition_product(config={"start_hour": 9, "end_hour": 17, "timezone": "UTC"}) as ids:
        enable_state(ids)
        night = datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc)
        d = reserve(ids, "a@acme-a.com", now=night)
        assert d.code == "outside_window" and d.retry_at == datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


def test_suppressed_email_and_domain_are_refused_terminally():
    with acquisition_product() as ids:
        enable_state(ids)
        outbound.suppress(ids["company_id"], "email", "Gone@Acme-A.com", "unsubscribe")
        outbound.suppress(ids["company_id"], "domain", "blocked-co.com", "manual")
        for email in ("gone@acme-a.com", "anyone@blocked-co.com"):
            d = reserve(ids, email)
            assert not d.allowed and d.code == "suppressed" and not d.defer


def test_suppress_is_idempotent_and_normalises_case():
    with acquisition_product() as ids:
        outbound.suppress(ids["company_id"], "email", "X@Y.com", "a")
        outbound.suppress(ids["company_id"], "email", "x@y.com", "b")
        with session_scope() as s:
            assert s.query(Suppression).filter_by(company_id=ids["company_id"]).count() == 1


def test_duplicate_first_contact_refused_but_failed_attempt_can_retry():
    with acquisition_product() as ids:
        enable_state(ids)
        d = reserve(ids, "a@acme-a.com")
        assert d.allowed
        dup = reserve(ids, "A@acme-a.com", now=NOW + timedelta(minutes=10))
        assert dup.code == "duplicate" and not dup.defer
        outbound.mark_failed(d.message_id, "temporary", "transient")
        assert reserve(ids, "a@acme-a.com", now=NOW + timedelta(minutes=20)).allowed          # a failed send did not contact them


def test_only_one_follow_up_per_address():
    with acquisition_product() as ids:
        enable_state(ids)
        sent(ids, reserve(ids, "a@acme-a.com"))
        f1 = reserve(ids, "a@acme-a.com", kind="follow_up", now=NOW + timedelta(days=3))
        assert f1.allowed
        sent(ids, f1, NOW + timedelta(days=3))
        assert reserve(ids, "a@acme-a.com", kind="follow_up", now=NOW + timedelta(days=9)).code == "duplicate"


def test_domain_cooldown_blocks_a_second_contact_at_the_same_company():
    with acquisition_product(config={"domain_cooldown_days": 30}) as ids:
        enable_state(ids)
        sent(ids, reserve(ids, "owner@bigco.com"))
        d = reserve(ids, "manager@bigco.com", now=NOW + timedelta(days=1))
        assert d.code == "domain_cooldown"
        assert reserve(ids, "manager@bigco.com", now=NOW + timedelta(days=31)).allowed
        # free-mail domains are not "a company"
        sent(ids, reserve(ids, "x@gmail.com", now=NOW + timedelta(days=40)), NOW + timedelta(days=40))
        assert reserve(ids, "y@gmail.com", now=NOW + timedelta(days=40, minutes=10)).allowed


def test_two_processes_cannot_both_take_the_last_slot():
    """The reservation is serialised by a Postgres advisory lock: concurrent workers cannot overshoot the cap."""
    with acquisition_product(config={"daily_cap": 1}) as ids:
        enable_state(ids)
        pids = [add_prospect(ids["company_id"], ids["product_id"], name=f"c{i}", email=f"c{i}@acme-{i}.com") for i in range(8)]
        results, barrier = [], threading.Barrier(8)

        def go(i):
            barrier.wait()
            results.append(reserve(ids, f"c{i}@acme-{i}.com", prospect=pids[i], autopilot=False))

        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in threads]; [t.join() for t in threads]
        assert sum(1 for r in results if r.allowed) == 1


def test_paused_state_blocks_every_send_including_manual():
    with acquisition_product() as ids:
        enable_state(ids)
        assert outbound.trip_breaker(ids["company_id"], ids["product_id"], "test pause") is True
        assert outbound.trip_breaker(ids["company_id"], ids["product_id"], "again") is False       # idempotent: one incident
        for autopilot in (True, False):
            d = reserve(ids, f"a{autopilot}@acme-a.com", autopilot=autopilot)
            assert d.code == "paused" and d.defer
        with session_scope() as s:
            assert s.query(Incident).filter_by(product_id=ids["product_id"], dedup_key="safety_pause", status="open").count() == 1
        assert outbound.resume(ids["product_id"]) is True and outbound.resume(ids["product_id"]) is False
        assert reserve(ids, "ok@acme-ok.com").allowed
        with session_scope() as s:
            assert s.query(Incident).filter_by(product_id=ids["product_id"], dedup_key="safety_pause", status="open").count() == 0


# ---- bounces / breakers ---------------------------------------------------------------------------------
DSN_HARD = """This is the mail system. Delivery to the following recipient failed permanently:
Final-Recipient: rfc822; dead@acme-dead.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to reach does not exist."""
DSN_SOFT = """Delivery incomplete. Final-Recipient: rfc822; full@acme-full.com
Status: 4.2.2  mailbox full, try again later"""


def test_parse_bounce_hard_soft_and_non_bounces():
    hard = outbound.parse_bounce("MAILER-DAEMON@mx.google.com", "Delivery Status Notification (Failure)", DSN_HARD)
    assert hard and hard.hard and hard.recipient == "dead@acme-dead.com"
    soft = outbound.parse_bounce("postmaster@x.com", "Undelivered Mail Returned to Sender", DSN_SOFT)
    assert soft and not soft.hard and soft.recipient == "full@acme-full.com"
    assert outbound.parse_bounce("pat@acme.com", "Re: Quick question", "Sounds interesting, call me") is None
    assert outbound.parse_bounce("pat@acme.com", "Out of office", "I am away until Monday") is None


def test_hard_bounce_suppresses_the_address_and_flags_the_prospect():
    with acquisition_product() as ids:
        enable_state(ids)
        pid = add_prospect(ids["company_id"], ids["product_id"], name="Dead", email="dead@acme-dead.com")
        d = reserve(ids, "dead@acme-dead.com", prospect=pid)
        sent(ids, d)
        assert outbound.record_bounce(ids["company_id"], "Dead@acme-dead.com", True, "550") == ids["product_id"]
        with session_scope() as s:
            m = s.get(OutboundMessage, d.message_id)
            assert m.status == "bounced" and m.bounce_type == "hard"
            p = s.get(Prospect, pid)
            assert p.do_not_contact and p.do_not_contact_reason == "hard bounce"
        assert reserve(ids, "dead@acme-dead.com", now=NOW + timedelta(days=60)).code == "suppressed"
        assert outbound.record_bounce(ids["company_id"], "never-sent@x.com", True, "x") is None


def test_bounce_rate_breaker_pauses_sending_with_one_critical_incident():
    with acquisition_product(config={"bounce_min_sample": 10, "bounce_pause_rate": 0.05}) as ids:
        enable_state(ids)
        for i in range(12):
            d = reserve(ids, f"u{i}@acme-{i}.com", autopilot=False, now=NOW + timedelta(minutes=i))
            sent(ids, d, NOW + timedelta(minutes=i))
            if i < 2:
                outbound.record_bounce(ids["company_id"], f"u{i}@acme-{i}.com", True, "550")
        reason = outbound.evaluate_health(ids["company_id"], ids["product_id"])
        assert reason and "hard-bounce rate" in reason
        assert outbound.evaluate_health(ids["company_id"], ids["product_id"])                # still unhealthy -- but see below: no second incident
        with session_scope() as s:
            assert s.get(AutopilotState, ids["product_id"]).paused_reason
            inc = s.query(Incident).filter_by(product_id=ids["product_id"], dedup_key="safety_pause").all()
            assert len(inc) == 1 and inc[0].severity == "critical"


def test_small_samples_do_not_trip_the_bounce_breaker():
    with acquisition_product(config={"bounce_min_sample": 10}) as ids:
        enable_state(ids)
        for i in range(4):
            d = reserve(ids, f"u{i}@acme-{i}.com", autopilot=False, now=NOW + timedelta(minutes=i)); sent(ids, d)
            outbound.record_bounce(ids["company_id"], f"u{i}@acme-{i}.com", True, "550")
        assert outbound.evaluate_health(ids["company_id"], ids["product_id"]) is None


def test_consecutive_systemic_failures_trip_the_breaker_but_recipient_errors_do_not():
    with acquisition_product(config={"consecutive_failure_pause": 3}) as ids:
        enable_state(ids)
        for i in range(3):
            outbound.mark_failed(reserve(ids, f"r{i}@acme-{i}.com", autopilot=False, now=NOW + timedelta(minutes=i)).message_id, "bad address", "recipient")
        assert outbound.evaluate_health(ids["company_id"], ids["product_id"]) is None
        for i in range(3, 6):
            outbound.mark_failed(reserve(ids, f"r{i}@acme-{i}.com", autopilot=False, now=NOW + timedelta(minutes=i)).message_id, "535 auth failed", "systemic")
        reason = outbound.evaluate_health(ids["company_id"], ids["product_id"])
        assert reason and "failed on our side" in reason


def test_complaint_rate_breaker():
    with acquisition_product(config={"complaint_min_sample": 10, "complaint_pause_rate": 0.1}) as ids:
        enable_state(ids)
        for i in range(12):
            pid = add_prospect(ids["company_id"], ids["product_id"], name=f"x{i}", email=f"x{i}@acme-{i}.com")
            sent(ids, reserve(ids, f"x{i}@acme-{i}.com", prospect=pid, autopilot=False, now=NOW + timedelta(minutes=i)))
            if i < 3:
                with session_scope() as s:
                    s.get(Prospect, pid).reply_classification = "unsubscribe"
        reason = outbound.evaluate_health(ids["company_id"], ids["product_id"])
        assert reason and "unsubscribed or reported spam" in reason


def test_stale_reservations_are_relabelled_but_still_count_toward_the_cap():
    with acquisition_product(config={"daily_cap": 1}) as ids:
        enable_state(ids)
        d = reserve(ids, "a@acme-a.com", now=NOW)
        assert outbound.mark_stale_reservations(NOW + timedelta(hours=1)) == 1
        with session_scope() as s:
            assert s.get(OutboundMessage, d.message_id).status == "unknown"
        assert reserve(ids, "b@acme-b.com", now=NOW + timedelta(hours=2)).code == "daily_cap"
