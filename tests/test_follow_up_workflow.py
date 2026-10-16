"""
The 'Follow-up' step of the Prospecting -> Sales -> Follow-up -> Customer
loop. Proves: a prospect stuck at 'outreach_sent' past the threshold gets a
real follow-up task created; a fresh one doesn't; and a follow-up, once
sent, doesn't get followed up again automatically (no infinite loop).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from packages.agents.departments.growth import GrowthAgent
from packages.agents.departments.sales import SalesAgent
from packages.core.context import Context
from packages.core.services import ApprovalService
from packages.db.models import (
    OutboundMessage,
    ActivityEvent, Agent, AgentRun, AuditLog, Company, FunnelEvent, Product,
    ProductAgentEnablement, ProductConfig, Prospect, Task,
)
from packages.db.session import session_scope
from packages.tools.outreach import LoggingOutreachProvider


@pytest.fixture()
def follow_up_setup():
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"followup-{uuid.uuid4().hex[:8]}", name="Follow-up Test")
        session.add(company)
        session.flush()
        product = Product(id=uuid.uuid4(), company_id=company.id, slug=f"followup-p-{uuid.uuid4().hex[:6]}", name="Product", lifecycle="active")
        session.add(product)
        session.flush()
        session.add(ProductConfig(product_id=product.id, config_version=1, sales={"physical_address": "123 Test St, Testville, TS 00000"}))
        growth_agent = session.query(Agent).filter_by(slug="growth").one()
        sales_agent = session.query(Agent).filter_by(slug="sales").one()
        session.add(ProductAgentEnablement(product_id=product.id, agent_id=sales_agent.id, enabled=True))

        stale_prospect = Prospect(company_id=company.id, product_id=product.id, company_name="Stale Co", contact_email="a@stale.invalid", dedup_key="stale.invalid", stage="outreach_sent")
        fresh_prospect = Prospect(company_id=company.id, product_id=product.id, company_name="Fresh Co", contact_email="b@fresh.invalid", dedup_key="fresh.invalid", stage="outreach_sent")
        session.add_all([stale_prospect, fresh_prospect])
        session.flush()

        # Stale: 'outreach_sent' funnel event backdated past the threshold.
        session.add(FunnelEvent(prospect_id=stale_prospect.id, product_id=product.id, from_stage="qualified", to_stage="outreach_sent", agent="sales", created_at=datetime.now(timezone.utc) - timedelta(days=10)))
        # Fresh: sent moments ago -- must NOT be followed up yet.
        session.add(FunnelEvent(prospect_id=fresh_prospect.id, product_id=product.id, from_stage="qualified", to_stage="outreach_sent", agent="sales", created_at=datetime.now(timezone.utc)))
        # Both really received a first email (follow-ups are only for people who actually did).
        for prospect, when in ((stale_prospect, datetime.now(timezone.utc) - timedelta(days=10)), (fresh_prospect, datetime.now(timezone.utc))):
            session.add(OutboundMessage(id=uuid.uuid4(), company_id=company.id, product_id=product.id, prospect_id=prospect.id, kind="first_contact",
                                        to_email=prospect.contact_email, to_domain=prospect.contact_email.split("@")[1], subject="s", status="sent",
                                        approval="founder", reserved_at=when, sent_at=when))

        ids = {
            "company_id": company.id, "product_id": product.id, "growth_agent_id": growth_agent.id,
            "sales_agent_id": sales_agent.id, "stale_prospect_id": stale_prospect.id, "fresh_prospect_id": fresh_prospect.id,
        }
        session.commit()

    yield ids

    with session_scope() as session:
        for pid in (ids["stale_prospect_id"], ids["fresh_prospect_id"]):
            session.query(FunnelEvent).filter_by(prospect_id=pid).delete(synchronize_session=False)
        session.query(Prospect).filter(Prospect.id.in_([ids["stale_prospect_id"], ids["fresh_prospect_id"]])).delete(synchronize_session=False)
        session.query(AgentRun).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(OutboundMessage).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ActivityEvent).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(AuditLog).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Task).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ProductAgentEnablement).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(ProductConfig).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Product).filter_by(id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Company).filter_by(id=ids["company_id"]).delete(synchronize_session=False)


def test_only_the_stale_prospect_gets_a_follow_up_task(follow_up_setup):
    ctx = Context(company_id=follow_up_setup["company_id"], agent_id=follow_up_setup["growth_agent_id"], product_id=follow_up_setup["product_id"])
    result = GrowthAgent().handle(ctx, goal="follow ups", context_refs={"action": "schedule_follow_ups"})

    assert result["follow_ups_scheduled"] == 1

    with session_scope() as session:
        stale = session.get(Prospect, follow_up_setup["stale_prospect_id"])
        fresh = session.get(Prospect, follow_up_setup["fresh_prospect_id"])
        assert stale.stage == "follow_up"
        assert fresh.stage == "outreach_sent", "must not touch a prospect that hasn't gone stale yet"

        task = session.query(Task).filter_by(company_id=follow_up_setup["company_id"]).one()
        assert task.context_refs["follow_up"] is True
        assert task.context_refs["prospect_id"] == str(follow_up_setup["stale_prospect_id"])


def test_diagnose_funnel_still_works_as_the_default_action(follow_up_setup):
    """Backward-compat check: existing callers that pass no action (or
    context_refs=None) at all must still get the original bottleneck logic."""
    ctx = Context(company_id=follow_up_setup["company_id"], agent_id=follow_up_setup["growth_agent_id"], product_id=follow_up_setup["product_id"])
    result = GrowthAgent().handle(ctx, goal="diagnose", context_refs=None)
    assert result["status"] == "done"
    assert "finding" in result


def test_a_sent_follow_up_does_not_trigger_a_second_automatic_follow_up(follow_up_setup):
    ctx = Context(company_id=follow_up_setup["company_id"], agent_id=follow_up_setup["growth_agent_id"], product_id=follow_up_setup["product_id"])
    GrowthAgent().handle(ctx, goal="follow ups", context_refs={"action": "schedule_follow_ups"})

    sales_ctx = Context(company_id=follow_up_setup["company_id"], agent_id=follow_up_setup["sales_agent_id"], product_id=follow_up_setup["product_id"])
    sales_agent = SalesAgent(LoggingOutreachProvider())
    refs = {"action": "draft_and_send", "prospect_id": str(follow_up_setup["stale_prospect_id"]), "follow_up": True}
    result_1 = sales_agent.handle(sales_ctx, goal="send follow-up", context_refs=refs)
    approval_id = uuid.UUID(result_1["approval_id"])
    ApprovalService.decide(approval_id, approved=True)
    refs["approved"] = True
    result_2 = sales_agent.handle(sales_ctx, goal="send follow-up", context_refs=refs)
    assert result_2["status"] == "done"
    assert result_2["follow_up"] is True

    with session_scope() as session:
        prospect = session.get(Prospect, follow_up_setup["stale_prospect_id"])
        assert prospect.stage == "follow_up", "must stay at follow_up -- not revert to outreach_sent and risk a second auto follow-up"

    # Running schedule_follow_ups again must NOT pick this prospect back up --
    # it's no longer at 'outreach_sent'.
    result_again = GrowthAgent().handle(ctx, goal="follow ups", context_refs={"action": "schedule_follow_ups"})
    assert result_again["follow_ups_scheduled"] == 0
