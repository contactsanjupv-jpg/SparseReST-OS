"""The founder's goal ("N customers in D days") is supplied, validated, started by enabling autopilot, and ACTUALLY CONTROLS the
campaign: completion/expiry stop planning and sending, pace is assessed from observed data, and nothing is hardcoded."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.core import autopilot, campaign, outbound
from packages.core.autopilot_config import AutopilotConfig
from packages.db.models import Approval, AutopilotState, FunnelEvent, Incident, OutboundMessage, Product, Prospect, Task
from packages.db.session import session_scope
from scripts import autopilot_ctl, configure_product
from tests.acq_helpers import Clock, FakeEnrichment, RecordingProvider, acquisition_product, lead, make_registry
from tests.helpers import add_prospect, company_with_products
from tests.test_acquisition_workflow import build_world


@pytest.fixture()
def clock(monkeypatch):
    c = Clock(); monkeypatch.setattr(outbound, "_now", c); monkeypatch.setattr(autopilot, "_now", c)
    return c


def slug(ids):
    with session_scope() as s:
        return s.get(Product, ids["product_id"]).slug


def cli(ids, *args):
    out = []
    code = autopilot_ctl.main(["--product", slug(ids), *args], out=out.append)
    return code, "\n".join(out)


def record_customer(ids, prospect_id, when=None):
    with session_scope() as s:
        s.add(FunnelEvent(prospect_id=prospect_id, product_id=ids["product_id"], from_stage="meeting_booked", to_stage="customer", agent="sales",
                          **({"created_at": when} if when else {})))


def real_contact(ids, prospect_id, email, kind="first_contact", status="sent"):
    with session_scope() as s:
        s.add(OutboundMessage(id=uuid.uuid4(), company_id=ids["company_id"], product_id=ids["product_id"], prospect_id=prospect_id, kind=kind, to_email=email,
                              to_domain=email.split("@")[1], subject="s", status=status, approval="founder", reserved_at=datetime.now(timezone.utc), sent_at=datetime.now(timezone.utc)))


# ---- 1. supplied by the founder, not hardcoded ------------------------------------------------------------------
def test_nothing_is_hardcoded_there_is_no_default_goal():
    assert "target_customers" not in AutopilotConfig().to_dict()
    with company_with_products() as ids:
        assert campaign.progress(ids["product_id"])["status"] == "unset"
        assert campaign.progress(ids["product_id"])["target"] is None


def test_autopilot_will_not_start_without_a_goal():
    with acquisition_product(enable=False, goal=None) as ids:
        rep = autopilot.readiness(ids["company_id"], ids["product_id"])
        assert "campaign_goal" in rep["blocking"] and not rep["ready"]
        with pytest.raises(autopilot.AutopilotError, match="goal set --customers"):
            autopilot.enable(ids["company_id"], ids["product_id"])


@pytest.mark.parametrize("customers,days", [(0, 30), (5, 0), (-1, 5), (5, 366), (1001, 30), ("five", 30), (5, None)])
def test_goal_validation_rejects_nonsense(customers, days):
    with acquisition_product(enable=False, goal=None) as ids:
        with pytest.raises(campaign.GoalError):
            campaign.set_goal(ids["product_id"], customers, days)


def test_any_goal_the_founder_supplies_is_used_and_goals_are_per_product():
    with company_with_products(n_products=2) as ids:
        a, b = ids["product_ids"]
        campaign.set_goal(a, 5, 30); campaign.set_goal(b, 3, 14)
        pa, pb = campaign.progress(a), campaign.progress(b)
        assert (pa["target"], pa["days"], pb["target"], pb["days"]) == (5, 30, 3, 14)
        campaign.set_goal(a, 12, 90)
        assert (campaign.progress(a)["target"], campaign.progress(a)["days"]) == (12, 90) and campaign.progress(b)["target"] == 3


def test_five_customers_in_thirty_days_via_the_cli_and_yaml(tmp_path):
    with acquisition_product(enable=False, goal=None) as ids:
        assert cli(ids, "goal", "set", "--customers", "5")[0] == 1                       # both numbers are required
        code, out = cli(ids, "goal", "set", "--customers", "5", "--days", "30")
        assert code == 0 and "5 customers in 30 days" in out and "starts when you enable" in out.lower() or "clock starts" in out
        assert campaign.progress(ids["product_id"])["status"] == "pending"
        from tests.acq_helpers import ICP, POSITIONING, SALES
        f = tmp_path / "c.yaml"
        f.write_text(yaml.safe_dump({"icp": ICP, "positioning": POSITIONING, "sales": SALES, "goal": {"customers": 7, "days": 21}}))
        assert configure_product.main([slug(ids), str(f)], out=lambda *_: None) == 0
        p = campaign.progress(ids["product_id"])
        assert (p["target"], p["days"]) == (7, 21)
        f.write_text(yaml.safe_dump({"icp": ICP, "positioning": POSITIONING, "sales": SALES, "goal": {"customers": "REPLACE_ME", "days": 30}}))
        assert configure_product.main([slug(ids), str(f)]) == 1


def test_legacy_target_customers_config_key_is_tolerated_but_ignored():
    cfg = AutopilotConfig.from_dict({"target_customers": 99, "daily_cap": 12})
    assert cfg.daily_cap == 12 and "target_customers" not in cfg.to_dict()


# ---- 2. the clock starts when autopilot is enabled; only new customers count --------------------------------------------
def test_clock_starts_on_enable_and_only_customers_after_the_start_count():
    with acquisition_product(enable=False, goal=(2, 30)) as ids:
        pid = add_prospect(ids["company_id"], ids["product_id"], name="Early", email="early@acme-a.com", stage="customer")
        record_customer(ids, pid)                                                          # a customer BEFORE the campaign began
        assert campaign.progress(ids["product_id"])["status"] == "pending"
        autopilot.enable(ids["company_id"], ids["product_id"])
        p = campaign.progress(ids["product_id"])
        assert p["status"] == "active" and p["customers"] == 0 and p["remaining"] == 2 and p["started_at"]
        pid2 = add_prospect(ids["company_id"], ids["product_id"], name="Late", email="late@acme-b.com", stage="customer")
        record_customer(ids, pid2)
        assert campaign.progress(ids["product_id"])["customers"] == 1


def test_setting_a_goal_while_running_restarts_the_count():
    with acquisition_product(goal=(3, 30)) as ids:
        pid = add_prospect(ids["company_id"], ids["product_id"], name="A", email="a@acme-a.com", stage="customer"); record_customer(ids, pid)
        assert campaign.progress(ids["product_id"])["customers"] == 1
        p = campaign.set_goal(ids["product_id"], 5, 30)
        assert p["status"] == "active" and p["customers"] == 0 and p["target"] == 5


# ---- 3. the planner and governor obey the goal --------------------------------------------------------------------------
def make_drafts_waiting(ids, clock):
    w = build_world(ids, clock, [lead(1), lead(2)], provider=RecordingProvider())
    w.pump(5)
    assert len(w.pending_approvals()) == 2
    return w


def test_reaching_the_target_stops_planning_cancels_unsent_drafts_and_blocks_sends(clock):
    with acquisition_product(goal=(1, 30)) as ids:
        w = make_drafts_waiting(ids, clock)
        pid = add_prospect(ids["company_id"], ids["product_id"], name="Won", email="won@acme-w.com", stage="customer"); record_customer(ids, pid)
        actions = w.tick()
        assert actions["campaign"]["status"] == "completed" and "completed" in actions["skipped"]
        assert "campaign is completed: no new outreach" in autopilot.get_status(w.cid, w.pid)["effective_state"]       # status says so, not just "ON"
        assert actions["cancelled_on_campaign_end"] == 2 and w.pending_approvals() == []
        with session_scope() as s:
            assert s.query(Incident).filter_by(product_id=w.pid, dedup_key="campaign_complete").count() == 1
            assert s.query(Task).filter_by(product_id=w.pid, status="cancelled").count() == 2
        w.tick(); w.tick()
        with session_scope() as s:
            assert s.query(Incident).filter_by(product_id=w.pid, dedup_key="campaign_complete").count() == 1          # reported once
        assert w.tick()["discovery_tasks"] == 0 and w.provider.messages == []
        d = outbound.reserve(company_id=w.cid, product_id=w.pid, prospect_id=None, task_id=None, to_email="x@acme-x.com", kind="first_contact",
                             subject="s", approval="auto", autopilot=True)
        assert d.code == "campaign_ended" and d.defer                                  # the governor backstop
        assert outbound.reserve(company_id=w.cid, product_id=w.pid, prospect_id=None, task_id=None, to_email="y@acme-y.com", kind="first_contact",
                                subject="s", approval="founder", autopilot=False).allowed      # a founder's own manual send is still the founder's call


def test_the_deadline_expires_the_campaign_and_extending_revives_it(clock):
    with acquisition_product(goal=(5, 10)) as ids:
        w = make_drafts_waiting(ids, clock)
        clock.advance(days=11)
        actions = w.tick()
        assert actions["campaign"]["status"] == "expired" and w.pending_approvals() == []
        with session_scope() as s:
            inc = s.query(Incident).filter_by(product_id=w.pid, dedup_key="campaign_expired").one()
            assert inc.severity == "warning" and "goal extend" in inc.action_required
        assert w.tick()["draft_tasks"] == 0
        assert "campaign_goal" in autopilot.readiness(w.cid, w.pid)["blocking"]          # an expired campaign cannot be (re)started silently
        p = campaign.extend(w.pid, 30)
        assert p["status"] == "active" and p["days"] == 40
        with session_scope() as s:
            assert s.query(Incident).filter_by(product_id=w.pid, dedup_key="campaign_expired", status="open").count() == 0
        assert w.tick()["campaign"]["status"] == "active"
        with pytest.raises(campaign.GoalError):
            campaign.extend(uuid.uuid4(), 5)


def test_a_new_goal_after_completion_starts_a_fresh_campaign(clock):
    with acquisition_product(goal=(1, 30)) as ids:
        w = build_world(ids, clock, [lead(1)])
        pid = add_prospect(ids["company_id"], ids["product_id"], name="Won", email="won@acme-w.com", stage="customer"); record_customer(ids, pid)
        w.tick()
        assert campaign.progress(w.pid)["status"] == "completed"
        with pytest.raises(campaign.GoalError, match="already reached"):
            campaign.extend(w.pid, 5)
        p = campaign.set_goal(w.pid, 3, 30)
        assert p["status"] == "active" and p["customers"] == 0 and w.tick()["campaign"]["status"] == "active"


# ---- 4. pace is assessed from OBSERVED data only ---------------------------------------------------------------------------
def backdate_start(ids, days):
    with session_scope() as s:
        st = s.get(AutopilotState, ids["product_id"])
        st.campaign_started_at = datetime.now(timezone.utc) - timedelta(days=days)


def test_pace_is_too_early_then_behind_then_on_track_never_invented():
    with acquisition_product(goal=(5, 30)) as ids:
        assert campaign.progress(ids["product_id"])["pace"] == "too_early"
        backdate_start(ids, 15)
        p = campaign.progress(ids["product_id"])
        assert p["pace"] == "at_risk" and p["projected_customers"] == 0.0 and p["status"] == "active"
        for i in range(2):
            pid = add_prospect(ids["company_id"], ids["product_id"], name=f"W{i}", email=f"w{i}@acme-{i}.com", stage="customer"); record_customer(ids, pid)
        p = campaign.progress(ids["product_id"])
        assert p["pace"] == "behind" and p["customers"] == 2                              # 2/15 days -> ~4 projected < 5 but >= 2.5
        for i in range(2, 4):
            pid = add_prospect(ids["company_id"], ids["product_id"], name=f"W{i}", email=f"w{i}@acme-{i}.com", stage="customer"); record_customer(ids, pid)
        assert campaign.progress(ids["product_id"])["pace"] == "on_track"


def test_pace_never_extrapolates_from_the_first_days():
    """Regression from the real run: 1 customer on day 0 said 'on track (projected ~30)'."""
    with acquisition_product(goal=(5, 30)) as ids:
        pid = add_prospect(ids["company_id"], ids["product_id"], name="Early", email="e@acme-e.com", stage="customer"); record_customer(ids, pid)
        p = campaign.progress(ids["product_id"])
        assert p["customers"] == 1 and p["pace"] == "too_early" and p["projected_customers"] is None
        backdate_start(ids, 3)
        assert campaign.progress(ids["product_id"])["pace"] == "too_early"
        backdate_start(ids, 8)
        assert campaign.progress(ids["product_id"])["projected_customers"] is not None


def test_falling_behind_raises_one_deduplicated_warning_that_clears_when_back_on_pace(clock):
    with acquisition_product(goal=(5, 30)) as ids:
        backdate_start(ids, 15)
        for _ in range(3):
            campaign.evaluate(ids["company_id"], ids["product_id"])
        with session_scope() as s:
            rows = s.query(Incident).filter_by(product_id=ids["product_id"], dedup_key="campaign_behind", status="open").all()
            assert len(rows) == 1 and rows[0].severity == "warning" and "days left" in rows[0].title
        for i in range(5):
            pid = add_prospect(ids["company_id"], ids["product_id"], name=f"W{i}", email=f"w{i}@acme-{i}.com", stage="customer"); record_customer(ids, pid)
        campaign.evaluate(ids["company_id"], ids["product_id"])                              # target met -> completes and clears the pace warning
        with session_scope() as s:
            assert s.query(Incident).filter_by(product_id=ids["product_id"], dedup_key="campaign_behind", status="open").count() == 0


def test_capacity_ceiling_and_required_close_rate_are_plain_arithmetic():
    cfg = AutopilotConfig.from_dict({"daily_cap": 20, "warmup": False, "send_days": [0, 1, 2, 3, 4], "timezone": "UTC"})
    start = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)                                  # a Monday
    assert campaign.capacity_ceiling(cfg, start, 7) == 5 * 20 and campaign.capacity_ceiling(cfg, start, 14) == 10 * 20
    warm = AutopilotConfig.from_dict({"daily_cap": 30, "warmup": True, "send_days": [0, 1, 2, 3, 4, 5, 6], "timezone": "UTC"})
    assert campaign.capacity_ceiling(warm, start, 21) == 7 * 10 + 7 * 20 + 7 * 30


def test_description_is_honest_about_ambition():
    with acquisition_product(goal=(50, 7)) as ids:
        text = campaign.progress(ids["product_id"])["message"]
        assert "50 customers in 7 days" in text and "usually converts far fewer" in text


def test_observed_close_rate_sizes_the_prospect_stock_the_planner_keeps(clock):
    """With real data (>=20 first emails and >=1 customer) the planner keeps enough prospects to reach the TARGET, not a fixed buffer."""
    with acquisition_product(goal=(5, 60), config={"daily_cap": 30, "buffer_days": 1.0}) as ids:
        w = build_world(ids, clock, [], provider=RecordingProvider())
        for i in range(35):                                                               # 35 qualified prospects already waiting: above the 30-prospect buffer
            with session_scope() as s:
                s.add(Prospect(company_id=w.cid, product_id=w.pid, company_name=f"Q{i}", dedup_key=f"q{i}.com", contact_email=f"q{i}@q{i}.com", stage="qualified", icp_score=0.9,
                               qualification={"passed": True, "failed": [], "checks": []}))
        assert w.tick()["discovery_tasks"] == 0                                           # stock covers the default buffer: no discovery
        with session_scope() as s:
            s.query(Task).filter(Task.product_id == w.pid).delete(synchronize_session=False)
        for i in range(20):                                                               # observed: 20 real first emails ...
            real_contact(ids, None, f"sent{i}@sent{i}.com")
        pid = add_prospect(ids["company_id"], ids["product_id"], name="W", email="w@acme-w.com", stage="customer"); record_customer(ids, pid)   # ... and 1 customer
        p = campaign.progress(w.pid)
        assert p["observed_close_rate"] == 0.05 and p["contacts_needed"] == 80             # 4 customers still needed / 5%
        assert w.tick()["discovery_tasks"] == 1                                           # the goal now demands MORE prospects than the buffer


# ---- 5. reports, status, API --------------------------------------------------------------------------------------------------
def test_report_and_status_show_the_founders_goal(clock):
    from packages.core import reporting
    with acquisition_product(goal=(5, 30)) as ids:
        code, out = cli(ids, "status")
        assert "Goal: 0 / 5 customers in 30 days" in out
        m = reporting.compute_metrics(ids["company_id"], ids["product_id"], clock.now - timedelta(days=1), clock.now)
        assert m["target_customers"] == 5 and m["customers_remaining"] == 5
        assert "GOAL: 0 / 5 customers in 30 days" in reporting.render_text("XFlow", "today", m, reporting.find_bottlenecks(m))


def test_goal_api_endpoints():
    with acquisition_product(enable=False, goal=None) as ids:
        c = TestClient(create_app()); pid = ids["product_id"]
        assert c.get(f"/autopilot/{pid}/goal").json()["status"] == "unset"
        assert c.post(f"/autopilot/{pid}/goal", json={"customers": 0, "days": 30}).status_code == 422
        r = c.post(f"/autopilot/{pid}/goal", json={"customers": 5, "days": 30}).json()
        assert r["status"] == "pending" and r["target"] == 5 and r["days"] == 30
        assert c.post(f"/autopilot/{pid}/goal/extend", json={"days": 5}).status_code == 409           # pending: nothing running to extend
        c.post(f"/autopilot/{pid}/enable", json={"mode": "approval"})
        assert c.post(f"/autopilot/{pid}/goal/extend", json={"days": 10}).json()["days"] == 40
        assert c.get(f"/autopilot/{pid}/status").json()["goal"]["target"] == 5
