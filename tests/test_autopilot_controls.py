"""Autopilot switch, readiness, approval->auto gating, configuration, API, CLI, reports, maintenance integration."""
from __future__ import annotations

import json
import textwrap
import uuid
from datetime import timedelta

import pytest
import yaml
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.agents.llm import LLMClient
from packages.core import autopilot, outbound, reporting
from packages.core.autopilot_config import ConfigError
from packages.core.config import get_settings
from packages.core.domains import StaticDomainChecker
from packages.core.maintenance import run_maintenance
from packages.core.services import ApprovalService
from packages.db.models import Approval, AutopilotState, GrowthReport, Product, ProductConfig, Prospect, Task
from packages.db.session import session_scope
from scripts import autopilot_ctl, configure_product
from tests.acq_helpers import ICP, POSITIONING, SALES, Clock, FakeEnrichment, RecordingProvider, acquisition_product, drain, lead, make_registry
from tests.helpers import company_with_products


@pytest.fixture()
def live_env(monkeypatch):
    """Simulate a fully configured live-sending deployment."""
    for k, v in {"OUTREACH_MODE": "smtp", "SMTP_HOST": "smtp.test", "SMTP_USERNAME": "u@xflow.example", "SMTP_PASSWORD": "pw",
                 "SMTP_FROM_EMAIL": "sam@xflow.example", "IMAP_HOST": "imap.test", "ENRICHMENT_MODE": "free", "REPORT_EMAIL_TO": "founder@xflow.example"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def checks(report):
    return {c["name"]: c for c in report["checks"]}


# ---- readiness ----------------------------------------------------------------------------------------------
def test_unconfigured_product_is_not_ready_and_cannot_be_enabled():
    with company_with_products() as ids:
        rep = autopilot.readiness(ids["company_id"], ids["product_id"])
        assert not rep["ready"] and {"icp_target", "qualification_policy", "positioning", "physical_address"} <= set(rep["blocking"])
        with pytest.raises(autopilot.AutopilotError, match="not ready"):
            autopilot.enable(ids["company_id"], ids["product_id"])


def test_default_stub_deployment_is_a_clearly_labelled_dry_run():
    with acquisition_product(enable=False) as ids:
        rep = autopilot.readiness(ids["company_id"], ids["product_id"])
        assert rep["ready"] and rep["dry_run"] and checks(rep)["dry_run"]["ok"]
        assert not checks(rep)["enrichment_live"]["ok"] and not checks(rep)["enrichment_live"]["blocking"]    # warning only


def test_live_sending_requires_credentials_inbound_and_spf(live_env):
    with acquisition_product(enable=False) as ids:
        no_spf = StaticDomainChecker(txt={})
        rep = autopilot.readiness(ids["company_id"], ids["product_id"], checker=no_spf)
        assert "sender_spf" in rep["blocking"] and not rep["ready"] and not rep["dry_run"]
        good = StaticDomainChecker(txt={"xflow.example": ["v=spf1 include:_spf.google.com ~all"], "_dmarc.xflow.example": ["v=DMARC1; p=none"]})
        rep = autopilot.readiness(ids["company_id"], ids["product_id"], checker=good)
        assert rep["ready"] and checks(rep)["sender_dmarc"]["ok"]


def test_live_sending_blocked_without_inbound_because_unsubscribes_could_not_be_honoured(live_env, monkeypatch):
    monkeypatch.delenv("IMAP_HOST"); get_settings.cache_clear()
    with acquisition_product(enable=False) as ids:
        rep = autopilot.readiness(ids["company_id"], ids["product_id"], checker=StaticDomainChecker(txt={"xflow.example": ["v=spf1 -all"]}))
        assert "inbound_configured" in rep["blocking"]


def test_missing_physical_address_blocks(live_env):
    with acquisition_product(sales={"sender_name": "Sam"}, enable=False) as ids:
        assert "physical_address" in autopilot.readiness(ids["company_id"], ids["product_id"])["blocking"]


# ---- enable / disable / mode ---------------------------------------------------------------------------------
def test_enable_disable_roundtrip_activates_product_lifecycle():
    with acquisition_product(enable=False) as ids:
        with session_scope() as s:
            s.get(Product, ids["product_id"]).lifecycle = "draft"
        st = autopilot.enable(ids["company_id"], ids["product_id"])
        assert st["enabled"] and st["mode"] == "approval" and "founder approves every email" in st["effective_state"]
        with session_scope() as s:
            assert s.get(Product, ids["product_id"]).lifecycle == "active"
        assert autopilot.disable(ids["company_id"], ids["product_id"])["effective_state"] == "OFF"


def test_auto_mode_is_gated_by_ack_live_sending_and_founder_track_record(live_env):
    good = StaticDomainChecker(txt={"xflow.example": ["v=spf1 -all"]})
    with acquisition_product(enable=False, config={"min_founder_approvals_for_auto": 3}) as ids:
        c, p = ids["company_id"], ids["product_id"]
        with pytest.raises(autopilot.AutopilotError, match="acknowledgement"):
            autopilot.enable(c, p, mode="auto", checker=good)
        with pytest.raises(autopilot.AutopilotError, match="approved 3 drafts yourself"):
            autopilot.enable(c, p, mode="auto", ack_auto=True, checker=good)
        assert autopilot.auto_send_allowed(c, p) is False
        _seed_approvals(ids, approved=3, rejected=0)
        st = autopilot.enable(c, p, mode="auto", ack_auto=True, checker=good)
        assert st["mode"] == "auto" and autopilot.auto_send_allowed(c, p) is True
        assert autopilot.set_mode(c, p, "approval")["mode"] == "approval"                   # downgrading is instant and unconditional
        assert autopilot.auto_send_allowed(c, p) is False


def test_auto_mode_refused_when_founder_rejects_too_many_drafts(live_env):
    good = StaticDomainChecker(txt={"xflow.example": ["v=spf1 -all"]})
    with acquisition_product(enable=False, config={"min_founder_approvals_for_auto": 3}) as ids:
        _seed_approvals(ids, approved=4, rejected=3)
        with pytest.raises(autopilot.AutopilotError, match="rejected"):
            autopilot.enable(ids["company_id"], ids["product_id"], mode="auto", ack_auto=True, checker=good)


def test_auto_mode_unavailable_in_dry_run():
    with acquisition_product(enable=False, config={"min_founder_approvals_for_auto": 1}) as ids:
        _seed_approvals(ids, approved=2, rejected=0)
        with pytest.raises(autopilot.AutopilotError, match="live sending"):
            autopilot.enable(ids["company_id"], ids["product_id"], mode="auto", ack_auto=True)


def _seed_approvals(ids, approved, rejected):
    with session_scope() as s:
        from packages.db.models import Agent
        agent = s.query(Agent).filter_by(slug="sales").one()
        for status, n in (("approved", approved), ("rejected", rejected)):
            for _ in range(n):
                t = Task(company_id=ids["company_id"], product_id=ids["product_id"], agent_id=agent.id, created_by="test", goal="g", status="done")
                s.add(t); s.flush()
                s.add(Approval(id=uuid.uuid4(), task_id=t.id, requested_by="sales", summary="s", risk_level="medium", status=status))


def test_auto_mode_actually_sends_without_approval_but_still_governed(live_env, monkeypatch):
    good = StaticDomainChecker(txt={"xflow.example": ["v=spf1 -all"]})
    clock = Clock()
    monkeypatch.setattr(outbound, "_now", clock); monkeypatch.setattr(autopilot, "_now", clock)
    with acquisition_product(enable=False, config={"min_founder_approvals_for_auto": 1, "daily_cap": 2, "min_gap_seconds": 60}) as ids:
        c, p = ids["company_id"], ids["product_id"]
        _seed_approvals(ids, approved=1, rejected=0)
        autopilot.enable(c, p, mode="auto", ack_auto=True, checker=good)
        provider = RecordingProvider()
        reg = make_registry(FakeEnrichment([lead(i) for i in range(1, 5)]), provider=provider)
        for _ in range(40):
            autopilot.tick(c, p, now=clock.now); drain(reg, p); clock.advance(minutes=3)
        assert len(provider.messages) == 2                                           # no approvals needed, yet the daily cap of 2 held
        with session_scope() as s:
            assert s.query(Approval).join(Task, Task.id == Approval.task_id).filter(Task.product_id == p, Approval.status == "pending").count() == 0
        from packages.db.models import OutboundMessage
        with session_scope() as s:
            assert {m.approval for m in s.query(OutboundMessage).filter_by(company_id=c, status="sent").all()} == {"auto"}


def test_a_task_labelled_auto_cannot_bypass_approval_when_the_mode_is_approval(monkeypatch):
    clock = Clock(); monkeypatch.setattr(outbound, "_now", clock); monkeypatch.setattr(autopilot, "_now", clock)
    with acquisition_product() as ids:
        provider = RecordingProvider()
        reg = make_registry(FakeEnrichment([lead(1)]), provider=provider)
        for _ in range(5):
            autopilot.tick(ids["company_id"], ids["product_id"], now=clock.now); drain(reg, ids["product_id"])
        assert provider.messages == []                                               # origin=autopilot tasks still wait for the founder


# ---- configuration ------------------------------------------------------------------------------------------
def test_update_config_validates_before_persisting():
    with acquisition_product(enable=False) as ids:
        with pytest.raises(ConfigError):
            autopilot.update_config(ids["company_id"], ids["product_id"], {"daily_cap": 500})
        assert autopilot.get_status(ids["company_id"], ids["product_id"])["config"]["daily_cap"] == 30
        assert autopilot.update_config(ids["company_id"], ids["product_id"], {"daily_cap": 12})["config"]["daily_cap"] == 12


def test_validate_acquisition_config_reports_every_problem_and_placeholders():
    probs = autopilot.validate_acquisition_config({}, {}, {})
    assert len(probs) >= 5
    ok = autopilot.validate_acquisition_config(ICP, POSITIONING, SALES)
    assert ok == []
    placeholder = autopilot.validate_acquisition_config(ICP, {**POSITIONING, "one_liner": "REPLACE_ME"}, SALES)
    assert any("REPLACE_ME" in p for p in placeholder)
    bad_q = autopilot.validate_acquisition_config({**ICP, "qualification": {"min_score": 2, "bogus": 1}}, POSITIONING, SALES)
    assert any("unknown" in p for p in bad_q) and any("min_score" in p for p in bad_q)


def test_apply_product_config_is_versioned_and_keeps_history():
    with company_with_products() as ids:
        v = autopilot.apply_product_config(ids["product_id"], icp=ICP, positioning=POSITIONING, sales=SALES)
        v2 = autopilot.apply_product_config(ids["product_id"], icp={**ICP, "geography": "Elsewhere"}, positioning=POSITIONING, sales=SALES)
        assert v2 == v + 1
        from packages.db.models import ProductConfigHistory
        with session_scope() as s:
            assert s.query(ProductConfigHistory).filter_by(product_id=ids["product_id"]).count() == 2
        with pytest.raises(ConfigError):
            autopilot.apply_product_config(ids["product_id"], icp={}, positioning={}, sales={})


def test_configure_product_script_dry_run_apply_and_errors(tmp_path):
    with company_with_products() as ids:
        slug = None
        with session_scope() as s:
            slug = s.get(Product, ids["product_id"]).slug
        good = tmp_path / "good.yaml"
        good.write_text(yaml.safe_dump({"icp": ICP, "positioning": POSITIONING, "sales": SALES, "autopilot": {"daily_cap": 15}}))
        out = []
        assert configure_product.main([slug, str(good), "--dry-run"], out=out.append) == 0 and "dry run" in out[0]
        with session_scope() as s:
            assert not s.get(ProductConfig, ids["product_id"]).icp                      # dry run wrote nothing
        assert configure_product.main([slug, str(good)], out=out.append) == 0
        assert autopilot.get_status(ids["company_id"], ids["product_id"])["config"]["daily_cap"] == 15
        template = tmp_path / "t.yaml"
        template.write_text(yaml.safe_dump({"icp": ICP, "positioning": {**POSITIONING, "one_liner": "REPLACE_ME"}, "sales": SALES}))
        assert configure_product.main([slug, str(template)]) == 1
        assert configure_product.main([slug, str(tmp_path / "missing.yaml")]) == 1
        assert configure_product.main(["no-such-product", str(good)]) == 1
        weird = tmp_path / "w.yaml"; weird.write_text(yaml.safe_dump({"icp": ICP, "positioning": POSITIONING, "sales": SALES, "typo_section": {}}))
        assert configure_product.main([slug, str(weird)]) == 1


def test_shipped_example_config_validates_only_after_placeholders_are_replaced():
    import pathlib
    data = yaml.safe_load(pathlib.Path("docs/xflow_config.example.yaml").read_text())
    probs = autopilot.validate_acquisition_config(data["icp"], data["positioning"], data["sales"])
    assert probs and all("REPLACE_ME" in p for p in probs), probs                       # the template cannot be applied by accident


# ---- CLI ----------------------------------------------------------------------------------------------------
def slug_of(ids):
    with session_scope() as s:
        return s.get(Product, ids["product_id"]).slug


def cli(ids, *args):
    out = []
    code = autopilot_ctl.main(["--product", slug_of(ids), *args], out=out.append)
    return code, "\n".join(out)


def test_cli_status_enable_disable_set_readiness():
    with acquisition_product(enable=False) as ids:
        code, out = cli(ids, "status"); assert code == 0 and "Autopilot: OFF" in out
        code, out = cli(ids, "readiness"); assert code == 0 and "READY" in out
        code, out = cli(ids, "set", "daily_cap=12", "start_hour=8"); assert code == 0 and "daily_cap=12" in out
        code, _ = cli(ids, "set", "daily_cap=999"); assert code == 1
        code, out = cli(ids, "enable"); assert code == 0 and "ON (founder approves" in out
        code, out = cli(ids, "disable"); assert "OFF" in out
        code, out = cli(ids, "resume"); assert code == 1 and "--ack" in out
        code, out = cli(ids, "resume", "--ack"); assert code == 0 and "not paused" in out


def test_cli_unready_product_fails_with_reasons():
    with company_with_products() as ids:
        code, out = cli(ids, "readiness")
        assert code == 1 and "BLOCKING" in out and "NOT READY" in out


def test_cli_approvals_list_approve_reject_and_suppress(monkeypatch):
    clock = Clock(); monkeypatch.setattr(outbound, "_now", clock); monkeypatch.setattr(autopilot, "_now", clock)
    with acquisition_product() as ids:
        reg = make_registry(FakeEnrichment([lead(1), lead(2), lead(3)]))
        for _ in range(5):
            autopilot.tick(ids["company_id"], ids["product_id"], now=clock.now); drain(reg, ids["product_id"])
        code, out = cli(ids, "approvals", "list")
        assert code == 0 and out.count("Subject:") == 3 and "100 Test Street" in out
        with session_scope() as s:
            pending = [str(a.id) for a in s.query(Approval).join(Task, Task.id == Approval.task_id).filter(Task.product_id == ids["product_id"], Approval.status == "pending").all()]
        code, out = cli(ids, "approvals", "reject", pending[0]); assert "Rejected 1" in out
        code, out = cli(ids, "approvals", "approve", "--all"); assert "Approved 2" in out
        code, out = cli(ids, "approvals", "approve"); assert "Nothing to do" in out
        code, out = cli(ids, "suppress", "add", "domain", "Bad-Co.com"); assert code == 0 and "bad-co.com" in out
        code, out = cli(ids, "suppress", "list"); assert "bad-co.com" in out and "domain" in out
        code, out = cli(ids, "suppress", "add"); assert code == 1


# ---- API -----------------------------------------------------------------------------------------------------
def test_autopilot_api_end_to_end(monkeypatch):
    with acquisition_product(enable=False) as ids:
        c = TestClient(create_app()); pid = ids["product_id"]
        st = c.get(f"/autopilot/{pid}/status").json()
        assert st["effective_state"] == "OFF" and st["readiness"]["ready"] is True
        assert c.post(f"/autopilot/{pid}/enable", json={"mode": "auto"}).status_code == 409
        assert c.post(f"/autopilot/{pid}/enable", json={"mode": "bogus"}).status_code == 422
        assert c.post(f"/autopilot/{pid}/enable", json={"mode": "approval"}).json()["enabled"] is True
        assert c.patch(f"/autopilot/{pid}/config", json={"settings": {"daily_cap": 20}}).json()["config"]["daily_cap"] == 20
        assert c.patch(f"/autopilot/{pid}/config", json={"settings": {"daily_cap": 31}}).status_code == 422
        assert c.post(f"/autopilot/{pid}/resume").status_code == 409
        outbound.trip_breaker(ids["company_id"], pid, "api test")
        assert "PAUSED" in c.get(f"/autopilot/{pid}/status").json()["effective_state"]
        assert c.post(f"/autopilot/{pid}/resume").json()["paused"] is None
        assert c.post(f"/autopilot/{pid}/disable").json()["enabled"] is False
        assert c.post(f"/autopilot/suppressions?product_id={pid}", json={"kind": "email", "value": "X@Y.com"}).status_code == 201
        assert c.get(f"/autopilot/suppressions?product_id={pid}").json()[0]["value"] == "x@y.com"
        assert c.get(f"/autopilot/{uuid.uuid4()}/status").status_code == 404


def test_bulk_approval_endpoint_only_touches_sales_outreach(monkeypatch):
    clock = Clock(); monkeypatch.setattr(outbound, "_now", clock); monkeypatch.setattr(autopilot, "_now", clock)
    with acquisition_product() as ids:
        reg = make_registry(FakeEnrichment([lead(1), lead(2)]))
        for _ in range(5):
            autopilot.tick(ids["company_id"], ids["product_id"], now=clock.now); drain(reg, ids["product_id"])
        with session_scope() as s:                                                    # an unrelated, non-outreach approval
            from packages.db.models import Agent
            agent = s.query(Agent).filter_by(slug="devops").one()
            t = Task(company_id=ids["company_id"], product_id=ids["product_id"], agent_id=agent.id, created_by="x", goal="deploy", status="needs_approval")
            s.add(t); s.flush()
            s.add(Approval(id=uuid.uuid4(), task_id=t.id, requested_by="devops", summary="Deploy to production", risk_level="high", status="pending"))
        c = TestClient(create_app())
        assert c.post("/approvals/bulk-decide", json={"approved": True}).status_code == 422
        listed = [a for a in c.get("/approvals?status=pending").json() if a["requested_by"] == "sales" and a["details"]]
        assert listed and "body" in listed[0]["details"]
        r = c.post("/approvals/bulk-decide", json={"approved": True, "all_pending_outreach": True, "product_id": str(ids["product_id"])}).json()
        assert r["decided"] == 2 and r["failed"] == []
        with session_scope() as s:
            assert s.query(Approval).filter_by(requested_by="devops", status="pending").count() >= 1      # the deploy is still waiting for a human


# ---- maintenance integration ------------------------------------------------------------------------------------
def test_maintenance_runs_autopilot_for_every_product_with_state_and_isolates_failures(monkeypatch):
    with acquisition_product() as good, acquisition_product(enable=False) as other:
        calls = []
        real_tick = autopilot.tick

        def flaky(company_id, product_id, **kw):
            calls.append(product_id)
            if product_id == other["product_id"]:
                raise RuntimeError("boom")
            return real_tick(company_id, product_id, **kw)
        monkeypatch.setattr(autopilot, "tick", flaky)
        summary = run_maintenance()
        assert {good["product_id"], other["product_id"]} <= set(calls)                 # one product's failure did not stop the other
        assert isinstance(summary["autopilot_tasks_created"], int)
        with session_scope() as s:
            assert s.query(Task).filter(Task.product_id == good["product_id"], Task.created_by == "autopilot").count() >= 1


# ---- LLM integration (when credentials exist) ---------------------------------------------------------------------------
class ScriptedLLM(LLMClient):
    def __init__(self, out):
        self.out, self.calls = out, 0

    def complete_json(self, *, system, prompt, model_tier="default"):
        self.calls += 1
        return self.out

    def complete_text(self, *, system, prompt, model_tier="default"):
        return ""


def test_llm_reply_classification_is_used_but_cannot_override_an_unsubscribe():
    from packages.agents.departments.sales import classify_reply
    llm = ScriptedLLM({"label": "interested"})
    assert classify_reply("Hmm, tell me more about pricing sometime next quarter", llm) == ("interested", "llm")
    assert classify_reply("please unsubscribe me", llm) == ("unsubscribe", "rules") and llm.calls == 1       # rules short-circuit: model never consulted
    assert classify_reply("random text", ScriptedLLM({"label": "invented_label"}))[1] == "rules"            # off-taxonomy answer rejected
    assert classify_reply("random text", ScriptedLLM(None))[1] == "rules"
    assert classify_reply("random text", None) == ("maybe", "rules")


def test_llm_icp_fit_check_can_disqualify_when_enabled():
    from tests.acq_helpers import FakeResearch
    icp = {**ICP, "qualification": {**ICP["qualification"], "llm_fit_prompt": "Independent shops with 1-10 employees, not national chains."}}
    clock = Clock()
    with acquisition_product(icp=icp) as ids:
        reg = make_registry(FakeEnrichment([lead(1), lead(2)]), llm=ScriptedLLM({"fit": False, "reason": "national chain"}))
        import packages.core.outbound as ob, packages.core.autopilot as ap
        for _ in range(4):
            ap.tick(ids["company_id"], ids["product_id"]); drain(reg, ids["product_id"])
        with session_scope() as s:
            ps = s.query(Prospect).filter_by(product_id=ids["product_id"]).all()
            assert ps and all(p.stage == "icp_match" and "llm_icp_fit" in p.qualification["failed"] for p in ps)


def test_cli_leads_meeting_and_customer_milestones(monkeypatch):
    from tests.test_acquisition_workflow import build_world
    clock = Clock(); monkeypatch.setattr(outbound, "_now", clock); monkeypatch.setattr(autopilot, "_now", clock)
    with acquisition_product() as ids:
        w = build_world(ids, clock, [lead(1), lead(2)])
        w.pump(5); w.approve_all(); w.send_everything_approved()
        w.reply("pat1@acme-plumbing-1.com", "Interested -- call me", uid="1")
        code, out = cli(ids, "leads")
        assert code == 0 and "high_intent" in out and "pat1@acme-plumbing-1.com" in out and "pat2" not in out
        assert cli(ids, "customer", "nobody@x.com")[0] == 1
        code, out = cli(ids, "meeting", "PAT1@acme-plumbing-1.com"); assert code == 0 and "meeting booked" in out
        code, out = cli(ids, "customer", "pat1@acme-plumbing-1.com"); assert code == 0 and "CUSTOMER" in out
        m = reporting.compute_metrics(w.cid, w.pid, clock.now - timedelta(days=1), clock.now + timedelta(days=1))
        assert m["meetings_booked"] == 1 and m["customers"] == 1 and m["cumulative"]["customers"] == 1
