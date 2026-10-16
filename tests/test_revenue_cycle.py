"""
Proves the core loop actually works end to end, using real DB writes and the
real orchestrator dispatch -- not mocked at the service layer, only at the
external-world boundary (StubEnrichmentTool / LoggingOutreachProvider, which
exist exactly so this can run for $0 with no credentials).

CEO -> creates a Task -> orchestrator dispatches it -> Prospecting runs ->
real Prospect rows + FunnelEvent rows + audit_log + activity_events exist.
Then Sales drafts/sends (approval-gated) and classifies a reply. Then Growth
computes a real bottleneck finding from the funnel_events that resulted.
"""
from __future__ import annotations

import uuid

import pytest

from packages.agents.departments.ceo import CEOAgent
from packages.agents.departments.growth import GrowthAgent
from packages.agents.departments.prospecting import ProspectingAgent
from packages.agents.departments.sales import SalesAgent
from packages.core.context import Context
from packages.core.orchestrator import default_agent_registry, run_pending_tasks
from packages.core.services import ApprovalService, TaskService
from packages.db.models import (
    ActivityEvent, Agent, AuditLog, Company, FunnelEvent, Product, ProductAgentEnablement,
    ProductConfig, Prospect, Task,
)
from packages.db.scoped_session import scoped_session
from packages.db.session import session_scope
from packages.tools.enrichment import StubEnrichmentTool
from packages.tools.execution import ToolPermissionError, call_tool
from packages.tools.outreach import LoggingOutreachProvider


@pytest.fixture()
def seeded_test_product():
    """A throwaway company + product with a REAL configured ICP (unlike the
    seeded XFlow placeholder) and every growth-department agent enabled --
    enough to run the full pipeline for real."""
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"revtest-{uuid.uuid4().hex[:8]}", name="Rev Test Co")
        session.add(company)
        session.flush()

        product = Product(id=uuid.uuid4(), company_id=company.id, slug=f"revtest-product-{uuid.uuid4().hex[:6]}", name="Rev Test Product", lifecycle="active")
        session.add(product)
        session.flush()
        session.add(ProductConfig(product_id=product.id, config_version=1, icp={"buyer_titles": ["Owner", "President"]}, sales={"physical_address": "123 Test St, Testville, TS 00000"}))

        agent_slugs = ["ceo", "growth", "prospecting", "sales"]
        agents = session.query(Agent).filter(Agent.slug.in_(agent_slugs)).all()
        assert len(agents) == 4, "seed script must have been run before the test suite (see scripts/seed.py)"
        for agent in agents:
            if not agent.is_company_level:
                session.add(ProductAgentEnablement(product_id=product.id, agent_id=agent.id, enabled=True))

        ceo_id = next(a.id for a in agents if a.slug == "ceo")
        ids = {"company_id": company.id, "product_id": product.id, "ceo_id": ceo_id, "product_slug": product.slug}
        session.commit()

    yield ids

    with session_scope() as session:
        from packages.db.models import AgentRun
        prospect_ids = [p.id for p in session.query(Prospect).filter_by(product_id=ids["product_id"]).all()]
        if prospect_ids:
            session.query(FunnelEvent).filter(FunnelEvent.prospect_id.in_(prospect_ids)).delete(synchronize_session=False)
        session.query(FunnelEvent).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        # company_id (not just product_id): a company-level agent's own activity (e.g. the
        # CEO's task.created event, product_id=NULL) belongs to this test's company too, and
        # would otherwise survive cleanup and block the final company delete via its FK.
        session.query(AgentRun).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ActivityEvent).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(AuditLog).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Task).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Prospect).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(ProductAgentEnablement).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(ProductConfig).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Product).filter_by(id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Company).filter_by(id=ids["company_id"]).delete(synchronize_session=False)


def test_ceo_to_prospecting_end_to_end(seeded_test_product):
    ceo_ctx = Context(company_id=seeded_test_product["company_id"], agent_id=seeded_test_product["ceo_id"], product_id=None)
    ceo_task_id = TaskService.create(ceo_ctx, goal=f"find 3 qualified prospects for {seeded_test_product['product_slug']}", created_by="founder")

    registry = default_agent_registry()
    registry["ceo"] = CEOAgent()

    ceo_result = registry["ceo"].execute(ceo_ctx, ceo_task_id, goal=f"find 3 qualified prospects for {seeded_test_product['product_slug']}")
    assert ceo_result["decomposition"] == "find_prospects"
    assert len(ceo_result["created_tasks"]) == 1

    run_results = run_pending_tasks(registry=registry, product_id=seeded_test_product["product_id"])
    assert len(run_results) == 1
    assert run_results[0]["status"] == "done"
    assert run_results[0]["result"]["created"] == 3

    with session_scope() as session:
        prospects = session.query(Prospect).filter_by(product_id=seeded_test_product["product_id"]).all()
        assert len(prospects) == 3
        assert all(p.contact_email_verified is False for p in prospects), "stub leads must never claim verified"

        funnel_rows = session.query(FunnelEvent).filter_by(product_id=seeded_test_product["product_id"]).all()
        assert len(funnel_rows) == 3

        audit_rows = session.query(AuditLog).filter_by(product_id=seeded_test_product["product_id"], tool="enrichment").all()
        assert len(audit_rows) == 1
        assert audit_rows[0].result == "success"

        event_rows = session.query(ActivityEvent).filter_by(product_id=seeded_test_product["product_id"], event_type="prospect.created").all()
        assert len(event_rows) == 3


def test_sales_agent_requires_approval_then_sends(seeded_test_product):
    prospecting_agent_id = _agent_id(seeded_test_product["product_id"], "prospecting")
    sales_agent_id = _agent_id(seeded_test_product["product_id"], "sales")

    with session_scope() as session:
        prospect = Prospect(
            company_id=seeded_test_product["company_id"], product_id=seeded_test_product["product_id"],
            company_name="Approval Test Co", contact_email="owner@approvaltest.invalid",
            contact_name="Pat Owner", dedup_key="approvaltest.invalid", stage="qualified",
        )
        session.add(prospect)
        session.flush()
        prospect_id = prospect.id

    sales_ctx = Context(company_id=seeded_test_product["company_id"], agent_id=sales_agent_id, product_id=seeded_test_product["product_id"])
    sales_agent = SalesAgent(LoggingOutreachProvider())

    task_id_1 = TaskService.create(sales_ctx, goal="draft outreach", created_by="growth", context_refs={"action": "draft_and_send", "prospect_id": str(prospect_id)})
    result_1 = sales_agent.execute(sales_ctx, task_id_1, goal="draft outreach", context_refs={"action": "draft_and_send", "prospect_id": str(prospect_id)})
    assert result_1["status"] == "needs_approval"
    approval_id = uuid.UUID(result_1["approval_id"])

    ApprovalService.decide(approval_id, approved=True)

    task_id_2 = TaskService.create(sales_ctx, goal="send outreach", created_by="growth", context_refs={"action": "draft_and_send", "prospect_id": str(prospect_id), "approved": True})
    result_2 = sales_agent.execute(sales_ctx, task_id_2, goal="send outreach", context_refs={"action": "draft_and_send", "prospect_id": str(prospect_id), "approved": True})
    assert result_2["status"] == "done"
    assert result_2["sent"] is False, "LoggingOutreachProvider never actually sends -- this proves no live send happened without real credentials"

    with session_scope() as session:
        p = session.get(Prospect, prospect_id)
        # A logged (dry-run) send is NOT a delivered email: the prospect must stay 'qualified', never 'outreach_sent'.
        assert p.stage == "qualified" and result_2["dry_run"] is True
        session.query(FunnelEvent).filter_by(prospect_id=prospect_id).delete()
        session.query(AuditLog).filter_by(task_id=task_id_2).delete()
        session.delete(p)


def test_sales_agent_classifies_high_intent_and_escalates(seeded_test_product):
    sales_agent_id = _agent_id(seeded_test_product["product_id"], "sales")
    with session_scope() as session:
        prospect = Prospect(
            company_id=seeded_test_product["company_id"], product_id=seeded_test_product["product_id"],
            company_name="Hot Lead Co", dedup_key="hotlead.invalid", stage="reply_received",
        )
        session.add(prospect)
        session.flush()
        prospect_id = prospect.id

    sales_ctx = Context(company_id=seeded_test_product["company_id"], agent_id=sales_agent_id, product_id=seeded_test_product["product_id"])
    sales_agent = SalesAgent(LoggingOutreachProvider())
    context_refs = {"action": "classify_reply", "prospect_id": str(prospect_id), "reply_text": "Yes, let's talk -- when can we start?"}
    task_id = TaskService.create(sales_ctx, goal="classify reply", created_by="webhook", context_refs=context_refs)
    result = sales_agent.execute(sales_ctx, task_id, goal="classify reply", context_refs=context_refs)

    assert result["classification"] == "high_intent"

    with session_scope() as session:
        from packages.db.models import Incident
        hot_events = session.query(ActivityEvent).filter_by(product_id=seeded_test_product["product_id"], event_type="sales.hot_lead").all()
        assert len(hot_events) == 1
        incidents = session.query(Incident).filter_by(product_id=seeded_test_product["product_id"], severity="warning").all()
        assert len(incidents) == 1, "a high-intent reply must create a real, trackable Incident -- not just an activity-feed entry"
        assert "Hot Lead Co" in incidents[0].title
        session.query(Incident).filter_by(id=incidents[0].id).delete()
        session.query(FunnelEvent).filter_by(prospect_id=prospect_id).delete()
        session.query(ActivityEvent).filter_by(product_id=seeded_test_product["product_id"]).delete()
        session.query(AuditLog).filter_by(task_id=task_id).delete()
        session.query(Prospect).filter_by(id=prospect_id).delete()


def test_growth_agent_flags_a_real_bottleneck(seeded_test_product):
    growth_agent_id = _agent_id(seeded_test_product["product_id"], "growth")
    prospect_ids = []
    with session_scope() as session:
        # 10 prospects reach 'qualified'; only 1 reaches 'researched' -- a real, sharp bottleneck.
        for i in range(11):
            prospect = Prospect(
                company_id=seeded_test_product["company_id"], product_id=seeded_test_product["product_id"],
                company_name=f"Funnel Test Co {i}", dedup_key=f"funneltest-{i}.invalid", stage="qualified",
            )
            session.add(prospect)
            session.flush()
            prospect_ids.append(prospect.id)

        for i in range(10):
            session.add(FunnelEvent(prospect_id=prospect_ids[i], product_id=seeded_test_product["product_id"], from_stage=None, to_stage="qualified", agent="prospecting"))
        session.add(FunnelEvent(prospect_id=prospect_ids[10], product_id=seeded_test_product["product_id"], from_stage="qualified", to_stage="researched", agent="sales"))

    ctx = Context(company_id=seeded_test_product["company_id"], agent_id=growth_agent_id, product_id=seeded_test_product["product_id"])
    result = GrowthAgent().handle(ctx, goal="diagnose funnel", context_refs=None)

    assert result["finding"] == "bottleneck"
    assert result["weakest_transition"]["from"] == "qualified"
    assert result["weakest_transition"]["conversion_rate"] < 0.2
    assert "do not respond" in result["note"].lower() or "diagnose before" in result["note"].lower()

    with session_scope() as session:
        session.query(FunnelEvent).filter(FunnelEvent.prospect_id.in_(prospect_ids)).delete(synchronize_session=False)
        session.query(Prospect).filter(Prospect.id.in_(prospect_ids)).delete(synchronize_session=False)


def test_xflow_seeded_placeholder_icp_blocks_prospecting():
    """Regression guard: the real seeded XFlow product must never let the
    Prospecting Agent run against its unconfirmed placeholder ICP."""
    with session_scope() as session:
        xflow = session.query(Product).filter_by(slug="xflow").one()
        prospecting_agent = session.query(Agent).filter_by(slug="prospecting").one()
        xflow_id, prospecting_id = xflow.id, prospecting_agent.id

    ctx = Context(company_id=None, agent_id=prospecting_id, product_id=xflow_id)
    # company_id isn't actually read for this check (RLS keys on product_id only for prospects
    # in this MVP), but Context requires a value -- fetch the real one to stay honest.
    with session_scope() as session:
        xflow = session.get(Product, xflow_id)
        ctx = Context(company_id=xflow.company_id, agent_id=prospecting_id, product_id=xflow_id)

    agent = ProspectingAgent(StubEnrichmentTool())
    result = agent.handle(ctx, goal="find prospects for xflow", context_refs=None)
    assert result["status"] == "blocked"
    assert result["created"] == 0


def test_unauthorized_tool_call_is_denied_and_audited(seeded_test_product):
    """A company-level agent (CEO) has no department tool grants -- calling
    'enrichment' must be denied, not silently allowed because it's the CEO."""
    ceo_ctx = Context(company_id=seeded_test_product["company_id"], agent_id=seeded_test_product["ceo_id"], product_id=None)
    with pytest.raises(ToolPermissionError):
        call_tool(ceo_ctx, "enrichment", lambda **kw: [], input={})

    with session_scope() as session:
        denied = session.query(AuditLog).filter_by(agent_id=seeded_test_product["ceo_id"], tool="enrichment", result="denied").all()
        assert len(denied) >= 1
        for row in denied:
            session.delete(row)


def _agent_id(product_id, slug) -> uuid.UUID:
    with session_scope() as session:
        return session.query(Agent).filter_by(slug=slug).one().id
