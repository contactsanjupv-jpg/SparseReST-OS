"""
process_inbound_replies() is the bridge: fetch real unread replies (via
ImapInboundChecker), match each to a real prospect by From-address, and
create a real classify_reply Task -- which the normal worker queue then
picks up like any other task, complete with retry/backoff and incident
reporting on failure. Nothing here classifies anything itself; that stays
SalesAgent's job, unchanged.

An unmatched reply (From-address doesn't match any known prospect for this
product) is not silently dropped -- it's logged as an info-level Incident,
since it usually means either a wrong-number bounce-adjacent reply or a
prospect whose email changed, both worth a human's eyes eventually, neither
urgent enough to page on.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from sqlalchemy import func

from packages.core.context import Context
from packages.core.incidents import IncidentService
from packages.core.services import TaskService
from packages.core.log import get_logger, log
from packages.db.models import Agent, Company, Prospect, Task
from packages.db.session import session_scope
from packages.tools.imap_inbound import ImapInboundChecker


def build_inbound_checker() -> Optional[ImapInboundChecker]:
    """Returns None if IMAP isn't configured -- callers should treat that as
    'inbound checking is off', not construct a broken checker. Falls back to
    the SMTP username/password when IMAP-specific ones aren't set separately,
    since sending and receiving through the same mailbox is the common case."""
    from packages.core.config import get_settings
    settings = get_settings()

    host = settings.imap_host
    username = settings.imap_username or settings.smtp_username
    password = settings.imap_password or settings.smtp_password
    if not (host and username and password):
        return None
    return ImapInboundChecker(host=host, username=username, password=password)


def process_inbound_replies(company_id: uuid.UUID, product_id: uuid.UUID, checker: ImapInboundChecker) -> list[uuid.UUID]:
    """Returns the task_ids created, one per matched reply. Safe to call
    repeatedly/on a schedule -- IMAP's \\Seen flag (set by the checker itself)
    is what prevents the same message from being processed twice, not
    anything tracked here."""
    with session_scope() as session:
        sales_agent_id = session.query(Agent).filter_by(slug="sales").one().id

    try:
        replies = checker.fetch_unread_replies()
    except Exception as exc:
        IncidentService().report(
            company_id=company_id, product_id=product_id, agent_id=sales_agent_id, task_id=None,
            severity="error", title="Inbound reply check failed",
            cause=str(exc), impact="New replies will not be detected until this is fixed.",
            action_required="Check the configured IMAP credentials/connectivity (IMAP_* settings).",
        )
        return []

    created_task_ids = []
    for reply in replies:
        with session_scope() as session:
            prospect = (
                session.query(Prospect)
                .filter_by(product_id=product_id)
                .filter(Prospect.contact_email.isnot(None))
                .filter(func.lower(Prospect.contact_email) == reply.from_email.strip().lower())
                .one_or_none()
            )
            prospect_id = prospect.id if prospect else None
            company_name = prospect.company_name if prospect else None

        if prospect_id is None:
            IncidentService().report(
                company_id=company_id, product_id=product_id, agent_id=sales_agent_id, task_id=None,
                severity="info", title=f"Unmatched inbound reply from {reply.from_email}",
                impact="This reply did not match any known prospect for this product.",
                action_required="Check manually -- possibly a new contact at a known company, or a stale email on file.",
            )
            continue

        ctx = Context(company_id=company_id, agent_id=sales_agent_id, product_id=product_id)
        task_id = TaskService.create(
            ctx, goal=f"Classify reply from {company_name}", created_by="inbound_email",
            context_refs={"action": "classify_reply", "prospect_id": str(prospect_id), "reply_text": reply.body},
        )
        created_task_ids.append(task_id)

    return created_task_ids


def run_inbound_check_loop(company_id: uuid.UUID, product_id: uuid.UUID, checker: ImapInboundChecker, *,
                            poll_interval_seconds: float = 60.0, max_iterations: Optional[int] = None) -> int:
    """A real background loop, same shape as packages/core/orchestrator.py's
    run_worker_loop -- max_iterations makes it directly testable (run N
    cycles and stop); omit it to run forever, which is how this would
    actually be deployed (a separate long-lived process, or the same one as
    the task worker if you prefer one process to operate)."""
    import time

    total_created = 0
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        created = process_inbound_replies(company_id, product_id, checker)
        total_created += len(created)
        if max_iterations is None:
            time.sleep(poll_interval_seconds)
    return total_created


# ---------------------------------------------------------------------------
# Company-level, crash-safe processing (what the production inbound process runs)
# ---------------------------------------------------------------------------
#
# The per-product function above has two limits that matter once there is more than one
# product or a real mailbox:
#   1. One mailbox serves every product. If product A's checker fetches (and marks Seen)
#      product B's replies, B's replies are lost AND reported as "unmatched". So the
#      production path fetches ONCE per company and routes each reply to whichever
#      product's prospect it matches.
#   2. Marking a message Seen before its task exists means a crash in between silently
#      drops a real customer reply. So here: create the task FIRST, mark Seen AFTER, and
#      de-duplicate on Message-ID so an unlucky crash between the two merely re-delivers,
#      never double-processes (at-least-once delivery + idempotent consumer).

_log = get_logger("inbound")
MAX_REPLY_BODY_CHARS = 20_000      # a reply is a few lines; cap what an untrusted sender can make us store/process


@dataclass
class InboundResult:
    task_ids: list[uuid.UUID] = field(default_factory=list)
    bounces: int = 0
    unmatched: int = 0
    duplicates: int = 0
    failed: int = 0


def resolve_company_id(slug: str = "sparserest") -> uuid.UUID:
    with session_scope() as session:
        company = session.query(Company).filter_by(slug=slug).one_or_none()
        if company is None:
            raise RuntimeError(f"Company '{slug}' not found -- run scripts/seed.py first")
        return company.id


def _already_processed(session, company_id: uuid.UUID, message_id: str) -> bool:
    if not message_id:
        return False
    return session.query(Task.id).filter(
        Task.company_id == company_id, Task.created_by == "inbound_email",
        Task.context_refs["message_id"].astext == message_id,
    ).first() is not None


def _route_reply(company_id: uuid.UUID, sales_agent_id: uuid.UUID, reply) -> tuple[str, Optional[uuid.UUID]]:
    """Returns ('task', task_id) | ('duplicate', None) | ('unmatched', None)."""
    sender = reply.from_email.strip().lower()
    with session_scope() as session:
        if _already_processed(session, company_id, reply.message_id):
            return "duplicate", None
        # The same address can exist under more than one product; the most recently touched prospect is the
        # one the person is almost certainly replying to.
        prospect = (
            session.query(Prospect)
            .filter(Prospect.company_id == company_id, func.lower(Prospect.contact_email) == sender)
            .order_by(Prospect.updated_at.desc())
            .first()
        )
        prospect_id = prospect.id if prospect else None
        product_id = prospect.product_id if prospect else None
        company_name = prospect.company_name if prospect else None

    if prospect_id is None:
        IncidentService().report(
            company_id=company_id, product_id=None, agent_id=sales_agent_id, task_id=None, severity="info",
            title=f"Unmatched inbound reply from {sender}",
            impact="This reply did not match any known prospect for any product.",
            action_required="Check manually -- possibly a new contact at a known company, a stale email on file, or a bounce notice.",
        )
        return "unmatched", None

    ctx = Context(company_id=company_id, agent_id=sales_agent_id, product_id=product_id)
    task_id = TaskService.create(
        ctx, goal=f"Classify reply from {company_name}", created_by="inbound_email",
        context_refs={"action": "classify_reply", "prospect_id": str(prospect_id),
                      "reply_text": (reply.body or "")[:MAX_REPLY_BODY_CHARS], "message_id": reply.message_id or None},
    )
    return "task", task_id


def process_inbound_for_company(company_id: uuid.UUID, checker) -> InboundResult:
    """Fetch once, route each reply to its product, then (and only then) mark handled messages Seen."""
    result = InboundResult()
    with session_scope() as session:
        sales_agent_id = session.query(Agent).filter_by(slug="sales").one().id

    try:
        replies = checker.fetch_unread_replies(mark_seen=False)
    except Exception as exc:
        log(_log, logging.ERROR, "inbound check failed", error=str(exc))
        IncidentService().report(
            company_id=company_id, product_id=None, agent_id=sales_agent_id, task_id=None, severity="error",
            title="Inbound reply check failed", cause=str(exc),
            impact="New replies will not be detected until this is fixed.",
            action_required="Check the configured IMAP credentials/connectivity (IMAP_* settings).",
        )
        result.failed += 1
        return result

    handled_uids: list[str] = []
    affected_products: set = set()
    for reply in replies:
        try:
            from packages.core import outbound
            bounce = outbound.parse_bounce(reply.from_email, reply.subject, reply.body)
            if bounce is not None:
                # A delivery-failure notice is not a customer reply. Recording it suppresses a dead address and
                # feeds the bounce-rate breaker that protects the sending domain.
                if bounce.recipient:
                    product_id = outbound.record_bounce(company_id, bounce.recipient, bounce.hard, bounce.detail)
                    if product_id:
                        affected_products.add(product_id)
                handled_uids.append(reply.imap_uid)
                result.bounces += 1
                continue
            kind, task_id = _route_reply(company_id, sales_agent_id, reply)
        except Exception as exc:
            # Leave this message UNSEEN so the next poll retries it -- a real reply is never dropped on an error.
            log(_log, logging.ERROR, "failed to process inbound reply; leaving unread for retry", error=str(exc), imap_uid=reply.imap_uid)
            result.failed += 1
            continue
        handled_uids.append(reply.imap_uid)
        if kind == "task":
            result.task_ids.append(task_id)
        elif kind == "duplicate":
            result.duplicates += 1
        else:
            result.unmatched += 1

    from packages.core import outbound as _outbound
    for product_id in affected_products:
        _outbound.evaluate_health(company_id, product_id)

    try:
        checker.mark_seen(handled_uids)
    except Exception as exc:
        # Safe: Message-ID de-duplication makes the re-delivery harmless. Worth knowing about, not worth paging.
        log(_log, logging.WARNING, "could not mark messages seen; they will be re-delivered and de-duplicated", error=str(exc))

    log(_log, logging.INFO, "inbound cycle complete", tasks_created=len(result.task_ids), bounces=result.bounces, unmatched=result.unmatched,
        duplicates=result.duplicates, failed=result.failed)
    return result
