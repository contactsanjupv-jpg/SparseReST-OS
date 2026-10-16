"""
Two real compliance requirements, not just documentation:

1. An 'unsubscribe' reply must make future contact technically impossible,
   not just discouraged -- checked as a hard block in _draft_and_send, not a
   stage that could be reset or bypassed by a retry/follow-up.
2. CAN-SPAM requires a physical mailing address in every commercial email --
   refusing to send without one configured, rather than sending a
   non-compliant message, mirrors the same guardrail pattern already used
   for ProspectingAgent's ICP check.
"""
from __future__ import annotations

import uuid

import pytest

from packages.agents.departments.sales import SalesAgent
from packages.core.context import Context
from packages.db.models import (
    ActivityEvent, Agent, AgentRun, AuditLog, Company, FunnelEvent, Product,
    ProductAgentEnablement, ProductConfig, Prospect, Task,
)
from packages.db.session import session_scope
from packages.tools.outreach import LoggingOutreachProvider


@pytest.fixture()
def compliance_setup():
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"compliance-{uuid.uuid4().hex[:8]}", name="Compliance Test")
        session.add(company)
        session.flush()
        product = Product(id=uuid.uuid4(), company_id=company.id, slug=f"compliance-p-{uuid.uuid4().hex[:6]}", name="Product", lifecycle="active")
        session.add(product)
        session.flush()
        session.add(ProductConfig(product_id=product.id, config_version=1))
        sales_agent = session.query(Agent).filter_by(slug="sales").one()
        session.add(ProductAgentEnablement(product_id=product.id, agent_id=sales_agent.id, enabled=True))

        prospect = Prospect(company_id=company.id, product_id=product.id, company_name="Compliance Co", contact_email="a@compliance.invalid", dedup_key="compliance.invalid", stage="qualified")
        session.add(prospect)
        session.flush()

        ids = {"company_id": company.id, "product_id": product.id, "sales_agent_id": sales_agent.id, "prospect_id": prospect.id}
        session.commit()

    yield ids

    with session_scope() as session:
        session.query(FunnelEvent).filter_by(prospect_id=ids["prospect_id"]).delete(synchronize_session=False)
        session.query(Prospect).filter_by(id=ids["prospect_id"]).delete(synchronize_session=False)
        session.query(AgentRun).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ActivityEvent).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(AuditLog).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Task).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ProductAgentEnablement).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(ProductConfig).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Product).filter_by(id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Company).filter_by(id=ids["company_id"]).delete(synchronize_session=False)


def test_send_is_refused_without_a_configured_physical_address(compliance_setup):
    ctx = Context(company_id=compliance_setup["company_id"], agent_id=compliance_setup["sales_agent_id"], product_id=compliance_setup["product_id"])
    result = SalesAgent(LoggingOutreachProvider()).handle(ctx, goal="send", context_refs={"action": "draft_and_send", "prospect_id": str(compliance_setup["prospect_id"])})
    assert result["status"] == "blocked"
    assert "physical_address" in result["reason"]


def test_send_succeeds_once_physical_address_is_configured_and_includes_it(compliance_setup):
    with session_scope() as session:
        config = session.get(ProductConfig, compliance_setup["product_id"])
        config.sales = {"physical_address": "742 Evergreen Terrace, Springfield, ST 00000"}

    ctx = Context(company_id=compliance_setup["company_id"], agent_id=compliance_setup["sales_agent_id"], product_id=compliance_setup["product_id"])
    agent = SalesAgent(LoggingOutreachProvider())
    refs = {"action": "draft_and_send", "prospect_id": str(compliance_setup["prospect_id"])}
    result_1 = agent.handle(ctx, goal="send", context_refs=refs)
    assert result_1["status"] == "needs_approval"

    from packages.core.services import ApprovalService
    ApprovalService.decide(uuid.UUID(result_1["approval_id"]), approved=True)
    refs["approved"] = True
    result_2 = agent.handle(ctx, goal="send", context_refs=refs)
    assert result_2["status"] == "done"


def test_unsubscribe_reply_creates_a_hard_unbypassable_block(compliance_setup):
    with session_scope() as session:
        config = session.get(ProductConfig, compliance_setup["product_id"])
        config.sales = {"physical_address": "742 Evergreen Terrace, Springfield, ST 00000"}
        session.get(Prospect, compliance_setup["prospect_id"]).stage = "reply_received"

    ctx = Context(company_id=compliance_setup["company_id"], agent_id=compliance_setup["sales_agent_id"], product_id=compliance_setup["product_id"])
    agent = SalesAgent(LoggingOutreachProvider())

    classify_result = agent.handle(ctx, goal="classify", context_refs={"action": "classify_reply", "prospect_id": str(compliance_setup["prospect_id"]), "reply_text": "please unsubscribe me from this list"})
    assert classify_result["classification"] == "unsubscribe"

    with session_scope() as session:
        prospect = session.get(Prospect, compliance_setup["prospect_id"])
        assert prospect.do_not_contact is True

    refs = {"action": "draft_and_send", "prospect_id": str(compliance_setup["prospect_id"]), "approved": True}
    send_result = agent.handle(ctx, goal="send anyway", context_refs=refs)
    assert send_result["status"] == "blocked"
    assert "opted out" in send_result["reason"].lower()
