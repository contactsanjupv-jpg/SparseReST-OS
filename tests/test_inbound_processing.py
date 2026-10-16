"""
Real DB, real matching logic, real Task rows -- only the IMAP connection
itself is faked (via a fake checker), consistent with every other
integration in this codebase.
"""
from __future__ import annotations

import uuid

import pytest

from packages.core.inbound import process_inbound_replies, run_inbound_check_loop
from packages.db.models import (
    ActivityEvent, AgentRun, AuditLog, Company, Incident, Product,
    ProductConfig, Prospect, Task,
)
from packages.db.session import session_scope
from packages.tools.imap_inbound import InboundReply


class FakeChecker:
    def __init__(self, replies):
        self._replies = replies
        self.call_count = 0

    def fetch_unread_replies(self):
        self.call_count += 1
        return self._replies


class ExplodingChecker:
    def fetch_unread_replies(self):
        raise ConnectionError("could not reach imap.example.com")


@pytest.fixture()
def inbound_setup():
    with session_scope() as session:
        company = Company(id=uuid.uuid4(), slug=f"inbound-{uuid.uuid4().hex[:8]}", name="Inbound Test")
        session.add(company)
        session.flush()
        product = Product(id=uuid.uuid4(), company_id=company.id, slug=f"inbound-p-{uuid.uuid4().hex[:6]}", name="Product", lifecycle="active")
        session.add(product)
        session.flush()
        session.add(ProductConfig(product_id=product.id, config_version=1))

        prospect = Prospect(company_id=company.id, product_id=product.id, company_name="Reply Test Co", contact_email="Owner@ReplyTest.invalid", dedup_key="replytest.invalid", stage="outreach_sent")
        session.add(prospect)
        session.flush()

        ids = {"company_id": company.id, "product_id": product.id, "prospect_id": prospect.id}
        session.commit()

    yield ids

    with session_scope() as session:
        session.query(Incident).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(AgentRun).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(ActivityEvent).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(AuditLog).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Task).filter_by(company_id=ids["company_id"]).delete(synchronize_session=False)
        session.query(Prospect).filter_by(id=ids["prospect_id"]).delete(synchronize_session=False)
        session.query(ProductConfig).filter_by(product_id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Product).filter_by(id=ids["product_id"]).delete(synchronize_session=False)
        session.query(Company).filter_by(id=ids["company_id"]).delete(synchronize_session=False)


def test_reply_matches_prospect_by_email_case_insensitively(inbound_setup):
    checker = FakeChecker([InboundReply(from_email="owner@replytest.invalid", subject="Re: hi", body="Sounds good, call me", imap_uid="1")])
    task_ids = process_inbound_replies(inbound_setup["company_id"], inbound_setup["product_id"], checker)

    assert len(task_ids) == 1
    with session_scope() as session:
        task = session.get(Task, task_ids[0])
        assert task.created_by == "inbound_email"
        assert task.context_refs["action"] == "classify_reply"
        assert task.context_refs["prospect_id"] == str(inbound_setup["prospect_id"])
        assert task.context_refs["reply_text"] == "Sounds good, call me"


def test_unmatched_reply_creates_an_info_incident_not_a_task(inbound_setup):
    checker = FakeChecker([InboundReply(from_email="nobody-we-know@elsewhere.invalid", subject="hi", body="who is this", imap_uid="1")])
    task_ids = process_inbound_replies(inbound_setup["company_id"], inbound_setup["product_id"], checker)

    assert task_ids == []
    with session_scope() as session:
        incidents = session.query(Incident).filter_by(company_id=inbound_setup["company_id"], severity="info").all()
        assert len(incidents) == 1
        assert "nobody-we-know@elsewhere.invalid" in incidents[0].title


def test_checker_failure_creates_an_error_incident_not_a_crash(inbound_setup):
    task_ids = process_inbound_replies(inbound_setup["company_id"], inbound_setup["product_id"], ExplodingChecker())

    assert task_ids == []
    with session_scope() as session:
        incidents = session.query(Incident).filter_by(company_id=inbound_setup["company_id"], severity="error").all()
        assert len(incidents) == 1
        assert "could not reach imap.example.com" in incidents[0].cause


def test_run_inbound_check_loop_processes_bounded_iterations(inbound_setup):
    checker = FakeChecker([InboundReply(from_email="owner@replytest.invalid", subject="hi", body="ok", imap_uid="1")])
    created = run_inbound_check_loop(inbound_setup["company_id"], inbound_setup["product_id"], checker, max_iterations=3)

    assert checker.call_count == 3
    assert created == 3

    with session_scope() as session:
        session.query(Task).filter_by(company_id=inbound_setup["company_id"]).delete()


def test_build_inbound_checker_returns_none_when_unconfigured(monkeypatch):
    from packages.core.config import get_settings
    from packages.core.inbound import build_inbound_checker

    for var in ("IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD", "SMTP_USERNAME", "SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    try:
        assert build_inbound_checker() is None
    finally:
        get_settings.cache_clear()


def test_build_inbound_checker_falls_back_to_smtp_credentials(monkeypatch):
    from packages.core.config import get_settings
    from packages.core.inbound import build_inbound_checker

    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "sales@sparserest.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    get_settings.cache_clear()
    try:
        checker = build_inbound_checker()
        assert checker is not None
        assert checker._username == "sales@sparserest.com"
    finally:
        for var in ("IMAP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        get_settings.cache_clear()
