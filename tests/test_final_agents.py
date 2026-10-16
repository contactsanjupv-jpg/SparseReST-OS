"""
The final six: Strategy (real cross-product volume comparison), Market
Intelligence (real per-competitor completeness audit), Product Management
(real feedback counts, never acting on one piece of feedback alone),
Operations (real SOP storage via knowledge_chunks), People/HR (never acts
autonomously -- the test IS that it always escalates), Legal (real substring
check against configured disallowed claims).
"""
from __future__ import annotations

import uuid

import pytest

from packages.agents.departments.legal import LegalAgent
from packages.agents.departments.market_intelligence import MarketIntelligenceAgent
from packages.agents.departments.operations import OperationsAgent
from packages.agents.departments.people_hr import PeopleHRAgent
from packages.agents.departments.product_management import ProductManagementAgent
from packages.agents.departments.strategy import StrategyAgent
from packages.core.context import Context
from packages.db.models import (
    ActivityEvent, Agent, AgentRun, Approval, AuditLog, Company, Feedback,
    KnowledgeChunk, Product, ProductAgentEnablement, ProductConfig,
    Prospect, RoadmapItem, Task,
)
from packages.db.session import session_scope


@pytest.fixture()
def final_agents_setup():
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"final-test-{uuid.uuid4().hex[:8]}", name="Final Agents Test Co")
        session.add(company)
        session.flush()

        product_a = Product(id=uuid.uuid4(), company_id=company.id, slug=f"final-a-{uuid.uuid4().hex[:6]}", name="A", lifecycle="active")
        product_b = Product(id=uuid.uuid4(), company_id=company.id, slug=f"final-b-{uuid.uuid4().hex[:6]}", name="B", lifecycle="active")
        session.add_all([product_a, product_b])
        session.flush()
        session.add(ProductConfig(product_id=product_a.id, config_version=1, competitors=[
            {"name": "WellKnown Co", "url": "https://wellknown.example", "differentiation": "we are faster"},
            {"name": "Mystery Co"},  # missing everything
        ]))
        session.add(ProductConfig(product_id=product_b.id, config_version=1, sales={"disallowed_claims": ["guaranteed results", "#1 in the industry"]}))

        slugs = ["strategy", "market_intelligence", "product_management", "operations", "people_hr", "legal"]
        for slug in slugs:
            agent = session.query(Agent).filter_by(slug=slug).one()
            if not agent.is_company_level:
                session.add(ProductAgentEnablement(product_id=product_a.id, agent_id=agent.id, enabled=True))
                session.add(ProductAgentEnablement(product_id=product_b.id, agent_id=agent.id, enabled=True))

        ids = {"company_id": company.id, "product_a_id": product_a.id, "product_b_id": product_b.id}
        for slug in slugs:
            ids[f"{slug}_id"] = session.query(Agent).filter_by(slug=slug).one().id
        session.commit()

    yield ids

    with session_scope() as session:
        for pid in (ids["product_a_id"], ids["product_b_id"]):
            session.query(Prospect).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(RoadmapItem).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(Feedback).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(ProductAgentEnablement).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(ProductConfig).filter_by(product_id=pid).delete(synchronize_session=False)
        session.query(AgentRun).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ActivityEvent).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(AuditLog).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Task).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Product).filter(Product.id.in_([ids["product_a_id"], ids["product_b_id"]])).delete(synchronize_session=False)
        session.query(Company).filter_by(id=ids["company_id"]).delete(synchronize_session=False)


def test_strategy_agent_flags_the_lower_volume_product(final_agents_setup):
    with session_scope() as session:
        for i in range(3):
            session.add(Prospect(company_id=final_agents_setup["company_id"], product_id=final_agents_setup["product_a_id"], company_name=f"A{i}", dedup_key=f"a{i}.invalid", stage="qualified"))
        session.add(Prospect(company_id=final_agents_setup["company_id"], product_id=final_agents_setup["product_b_id"], company_name="B0", dedup_key="b0.invalid", stage="qualified"))

    ctx = Context(company_id=final_agents_setup["company_id"], agent_id=final_agents_setup["strategy_id"], product_id=None)
    result = StrategyAgent().handle(ctx, goal="prioritize", context_refs=None)

    assert result["lowest_volume_product"] is not None
    counts = result["qualified_prospect_counts_by_product"]
    assert len(counts) == 2
    assert min(counts, key=counts.get) == result["lowest_volume_product"]


def test_market_intelligence_flags_under_researched_competitor(final_agents_setup):
    ctx = Context(company_id=final_agents_setup["company_id"], agent_id=final_agents_setup["market_intelligence_id"], product_id=final_agents_setup["product_a_id"])
    result = MarketIntelligenceAgent().handle(ctx, goal="audit competitors", context_refs=None)

    assert "WellKnown Co" in result["well_researched"]
    assert any(c["name"] == "Mystery Co" for c in result["under_researched"])


def test_product_management_never_triages_below_threshold(final_agents_setup):
    ctx = Context(company_id=final_agents_setup["company_id"], agent_id=final_agents_setup["product_management_id"], product_id=final_agents_setup["product_a_id"])
    agent = ProductManagementAgent()

    # Only one piece of feedback in this category -- must NOT become a roadmap item.
    r1 = agent.handle(ctx, goal="record", context_refs={"action": "record_feedback", "source": "customer", "content": "wish it had X", "category": "feature_x"})
    assert r1["status"] == "done"

    triage_1 = agent.handle(ctx, goal="triage", context_refs={"action": "triage"})
    assert triage_1["proposals"] == []
    assert triage_1["below_threshold"]["feature_x"] == 1

    # A second, independent piece of feedback in the same category crosses the threshold.
    agent.handle(ctx, goal="record", context_refs={"action": "record_feedback", "source": "prospect", "content": "please add X", "category": "feature_x"})
    triage_2 = agent.handle(ctx, goal="triage", context_refs={"action": "triage"})
    assert len(triage_2["proposals"]) == 1
    assert triage_2["proposals"][0]["category"] == "feature_x"
    assert triage_2["proposals"][0]["feedback_count"] == 2

    with session_scope() as session:
        session.query(RoadmapItem).filter_by(product_id=final_agents_setup["product_a_id"]).delete()
        session.query(Feedback).filter_by(product_id=final_agents_setup["product_a_id"]).delete()


def test_operations_agent_records_and_lists_sops(final_agents_setup):
    ctx = Context(company_id=final_agents_setup["company_id"], agent_id=final_agents_setup["operations_id"], product_id=None)
    agent = OperationsAgent()

    record_result = agent.handle(ctx, goal="record sop", context_refs={"action": "record_sop", "content": "How to onboard a new outreach domain: ..."})
    assert record_result["status"] == "done"

    list_result = agent.handle(ctx, goal="list sops", context_refs={"action": "list_sops"})
    assert list_result["count"] >= 1
    assert any("onboard a new outreach domain" in s["content"] for s in list_result["sops"])

    with session_scope() as session:
        session.query(KnowledgeChunk).filter_by(id=uuid.UUID(record_result["sop_id"])).delete()


def test_people_hr_agent_never_acts_autonomously(final_agents_setup):
    """The entire point of this agent: no matter the goal, it escalates."""
    ctx = Context(company_id=final_agents_setup["company_id"], agent_id=final_agents_setup["people_hr_id"], product_id=None)
    result = PeopleHRAgent().handle(ctx, goal="fire someone", context_refs=None)
    assert result["status"] == "needs_approval"

    with session_scope() as session:
        approval = session.get(Approval, uuid.UUID(result["approval_id"]))
        assert approval is not None
        assert approval.status == "pending"
        session.delete(approval)


def test_legal_agent_flags_a_disallowed_claim(final_agents_setup):
    ctx = Context(company_id=final_agents_setup["company_id"], agent_id=final_agents_setup["legal_id"], product_id=final_agents_setup["product_b_id"])
    result = LegalAgent().handle(ctx, goal="review copy", context_refs={"text": "We offer guaranteed results for every customer!"})
    assert result["clear"] is False
    assert "guaranteed results" in result["flagged_phrases"]


def test_legal_agent_passes_clean_copy(final_agents_setup):
    ctx = Context(company_id=final_agents_setup["company_id"], agent_id=final_agents_setup["legal_id"], product_id=final_agents_setup["product_b_id"])
    result = LegalAgent().handle(ctx, goal="review copy", context_refs={"text": "We'd love to show you how this could help your team."})
    assert result["clear"] is True
    assert result["flagged_phrases"] == []


def test_sales_mark_customer_completes_the_pipeline(final_agents_setup):
    """The last link in Prospecting -> Sales -> Follow-up -> Customer: a
    founder-confirmed transition, never triggered by an agent on its own."""
    from packages.agents.departments.sales import SalesAgent
    from packages.db.models import FunnelEvent, Prospect
    from packages.tools.outreach import LoggingOutreachProvider

    with session_scope() as session:
        prospect = Prospect(
            company_id=final_agents_setup["company_id"], product_id=final_agents_setup["product_a_id"],
            company_name="About To Buy Co", dedup_key="abouttobuy.invalid", stage="proposal_sent",
        )
        session.add(prospect)
        session.flush()
        prospect_id = prospect.id

    sales_agent_id_row = None
    with session_scope() as session:
        sales_agent_id_row = session.query(Agent).filter_by(slug="sales").one().id

    ctx = Context(company_id=final_agents_setup["company_id"], agent_id=sales_agent_id_row, product_id=final_agents_setup["product_a_id"])
    result = SalesAgent(LoggingOutreachProvider()).handle(ctx, goal="mark customer", context_refs={"action": "mark_customer", "prospect_id": str(prospect_id), "external": True})

    assert result["status"] == "done"
    assert result["new_stage"] == "customer"

    with session_scope() as session:
        p = session.get(Prospect, prospect_id)
        assert p.stage == "customer"
        fe = session.query(FunnelEvent).filter_by(prospect_id=prospect_id, to_stage="customer").one()
        assert fe.from_stage == "proposal_sent"
        session.query(FunnelEvent).filter_by(prospect_id=prospect_id).delete()
        session.query(Prospect).filter_by(id=prospect_id).delete()


def test_sales_mark_meeting_booked_is_founder_confirmed_only(final_agents_setup):
    """The middle-of-pipeline sibling to mark_customer -- also never
    triggered automatically."""
    from packages.agents.departments.sales import SalesAgent
    from packages.db.models import FunnelEvent, Prospect
    from packages.tools.outreach import LoggingOutreachProvider

    with session_scope() as session:
        prospect = Prospect(
            company_id=final_agents_setup["company_id"], product_id=final_agents_setup["product_a_id"],
            company_name="Ready To Meet Co", dedup_key="readytomeet.invalid", stage="reply_classified",
        )
        session.add(prospect)
        session.flush()
        prospect_id = prospect.id
        sales_agent_id_row = session.query(Agent).filter_by(slug="sales").one().id

    ctx = Context(company_id=final_agents_setup["company_id"], agent_id=sales_agent_id_row, product_id=final_agents_setup["product_a_id"])
    result = SalesAgent(LoggingOutreachProvider()).handle(ctx, goal="mark meeting booked", context_refs={"action": "mark_meeting_booked", "prospect_id": str(prospect_id), "external": True})

    assert result["status"] == "done"
    assert result["new_stage"] == "meeting_booked"

    with session_scope() as session:
        p = session.get(Prospect, prospect_id)
        assert p.stage == "meeting_booked"
        fe = session.query(FunnelEvent).filter_by(prospect_id=prospect_id, to_stage="meeting_booked").one()
        assert fe.from_stage == "reply_classified"
        session.query(FunnelEvent).filter_by(prospect_id=prospect_id).delete()
        session.query(Prospect).filter_by(id=prospect_id).delete()
