"""Shared fixtures-as-functions so new test files don't each re-invent company/product setup and teardown."""
from __future__ import annotations

import uuid
from contextlib import contextmanager

from packages.db.models import (
    ActivityEvent, AgentRun, Approval, AuditLog, AutopilotState, Company, FunnelEvent, GrowthReport, Incident,
    OutboundMessage, Product, ProductAgentEnablement, ProductConfig, ProductConfigHistory, Prospect, Suppression, Task,
)
from packages.db.session import session_scope


def create_company_with_products(n_products: int = 1, lifecycle: str = "active") -> dict:
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"t-co-{uuid.uuid4().hex[:8]}", name="Test Co")
        session.add(company)
        session.flush()
        product_ids = []
        for i in range(n_products):
            product = Product(id=uuid.uuid4(), company_id=company.id, slug=f"t-p{i}-{uuid.uuid4().hex[:6]}", name=f"Product {i}", lifecycle=lifecycle)
            session.add(product)
            session.flush()
            session.add(ProductConfig(product_id=product.id, config_version=1))
            product_ids.append(product.id)
        ids = {"company_id": company.id, "product_ids": product_ids, "product_id": product_ids[0]}
    return ids


def add_prospect(company_id, product_id, *, name="Acme", email=None, stage="outreach_sent", dedup=None) -> uuid.UUID:
    with session_scope() as session:
        prospect = Prospect(
            id=uuid.uuid4(), company_id=company_id, product_id=product_id, company_name=name,
            contact_email=email, dedup_key=dedup or f"{name.lower()}-{uuid.uuid4().hex[:8]}.test", stage=stage,
        )
        session.add(prospect)
        session.flush()
        return prospect.id


def cleanup_company(ids: dict) -> None:
    company_id = ids["company_id"]
    with session_scope() as session:
        for model in (Incident, AgentRun, ActivityEvent, AuditLog):
            session.query(model).filter_by(company_id=company_id).delete(synchronize_session=False)
        task_ids = [t.id for t in session.query(Task.id).filter_by(company_id=company_id).all()]
        if task_ids:
            session.query(Task).filter(Task.id.in_(task_ids)).update({"approval_id": None}, synchronize_session=False)
            session.query(Approval).filter(Approval.task_id.in_(task_ids)).delete(synchronize_session=False)
        session.query(Task).filter_by(company_id=company_id).delete(synchronize_session=False)
        for model in (OutboundMessage, Suppression):
            session.query(model).filter_by(company_id=company_id).delete(synchronize_session=False)
        for pid in ids["product_ids"]:
            session.query(GrowthReport).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(AutopilotState).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(ProductConfigHistory).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(FunnelEvent).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(Prospect).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(ProductAgentEnablement).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(ProductConfig).filter_by(product_id=pid).delete(synchronize_session=False)
            session.query(Product).filter_by(id=pid).delete(synchronize_session=False)
        session.query(Company).filter_by(id=company_id).delete(synchronize_session=False)


@contextmanager
def company_with_products(n_products: int = 1, lifecycle: str = "active"):
    ids = create_company_with_products(n_products, lifecycle)
    try:
        yield ids
    finally:
        cleanup_company(ids)
