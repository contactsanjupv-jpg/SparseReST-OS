"""
Phase 1 foundation schema.

One responsibility: define the durable operational state. No business logic
lives here -- that's in packages/agents and packages/core/services.py. See
docs/ARCHITECTURE.md for the reasoning behind each table; this file is the
literal source of truth for what's actually deployed (kept in sync via
Alembic migrations, never hand-edited on a live database).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# ---------------------------------------------------------------------------
# Company / Product registry
# ---------------------------------------------------------------------------

class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Agent(Base):
    """The agent registry -- one row per agent *implementation*, shared across
    every product. Which products a given agent is switched on for lives in
    ProductAgentEnablement, not here."""
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)   # 'prospecting', 'sales', 'ceo', ...
    name: Mapped[str] = mapped_column(String, nullable=False)
    department: Mapped[str] = mapped_column(String, nullable=False)
    is_company_level: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reports_to_slug: Mapped[str | None] = mapped_column(String, nullable=True)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("company_id", "slug", name="uq_products_company_slug"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    brand_assets: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    lifecycle: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    config: Mapped["ProductConfig"] = relationship(back_populates="product", uselist=False)


LIFECYCLE_STATES = ("draft", "configuring", "ready", "beta", "active", "paused", "sunset")
# States in which a product-level agent may take autonomous outreach/sales action.
LIFECYCLE_AUTONOMOUS_STATES = ("beta", "active")


class ProductConfig(Base):
    __tablename__ = "product_config"

    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), primary_key=True)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    icp: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    positioning: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pricing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    competitors: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    sales: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    marketing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    customer_success: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    engineering: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    billing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    knowledge_refs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    product: Mapped["Product"] = relationship(back_populates="config")


class ProductConfigHistory(Base):
    __tablename__ = "product_config_history"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    changed_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductIntegration(Base):
    __tablename__ = "product_integrations"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    credentials_ref: Mapped[str] = mapped_column(String, nullable=False)  # name in secrets manager/env -- NEVER a plaintext secret
    scopes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    health_status: Mapped[str] = mapped_column(String, default="unknown")


class ProductAgentEnablement(Base):
    __tablename__ = "product_agent_enablement"

    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("agents.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# ---------------------------------------------------------------------------
# Tasks / workflows / agent runs / events / audit
# ---------------------------------------------------------------------------

class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False)   # agent slug or 'founder'
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    context_refs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String, default="pending")
    # pending -> in_progress -> done | failed | needs_approval
    approval_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("approvals.id"), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Durable job-queue fields (see packages/core/queue.py). Tasks ARE the job
    # records -- there is no separate jobs table.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


TASK_STATUSES = ("pending", "in_progress", "done", "failed", "needs_approval", "cancelled")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String, nullable=False, default="idle")
    # idle|thinking|working|waiting|needs_approval|error|done|blocked
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


AGENT_RUN_STATUSES = ("idle", "thinking", "working", "waiting", "needs_approval", "error", "done", "blocked")


class ActivityEvent(Base):
    """Every visual-world-facing event. This table is what Phase 8's Playground/
    Company World will subscribe to -- it exists from Phase 1 specifically so
    the frontend is the only thing left to build when that phase starts."""
    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("companies.id"), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    tool: Mapped[str] = mapped_column(String, nullable=False)
    input: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[str] = mapped_column(String, nullable=False)   # 'success' | 'error' | 'denied'
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    approval_status: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")   # pending -> approved | rejected
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)   # what exactly is being approved (e.g. the full email)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Incidents (foundation for Phase 5 observability -- table exists now so
# Phase 5 is an alerting-logic project, not a schema project)
# ---------------------------------------------------------------------------

class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    severity: Mapped[str] = mapped_column(String, nullable=False)   # info | warning | error | critical
    title: Mapped[str] = mapped_column(String, nullable=False)
    cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="open")     # open | acknowledged | resolved
    action_required: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String, nullable=True)     # one OPEN incident per (product, dedup_key)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


SEVERITIES = ("info", "warning", "error", "critical")


# ---------------------------------------------------------------------------
# Revenue engine: prospects, funnel, experiments (minimal Phase-1 shape;
# extended when Phase 4 wires the real Prospecting/Sales tools)
# ---------------------------------------------------------------------------

class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    company_name: Mapped[str] = mapped_column(String, nullable=False)
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_title: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_email_source: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_email_confidence: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)
    contact_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    icp_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    dedup_key: Mapped[str] = mapped_column(String, nullable=False)   # normalized domain or name+geo, enforced unique per product
    stage: Mapped[str] = mapped_column(String, nullable=False, default="market_discovery")
    do_not_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    do_not_contact_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    qualification: Mapped[dict | None] = mapped_column(JSONB, nullable=True)   # hard-gate result: passed, score, per-check detail
    research: Mapped[dict | None] = mapped_column(JSONB, nullable=True)        # grounded facts from the prospect's own public website
    reply_classification: Mapped[str | None] = mapped_column(String, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("product_id", "dedup_key", name="uq_prospects_product_dedup"),)


PROSPECT_STAGES = (
    "market_discovery", "icp_match", "company_found", "decision_maker_found",
    "enriched", "qualified", "researched", "personalized", "outreach_sent",
    "reply_received", "reply_classified", "objection_handled", "follow_up",
    "meeting_booked", "proposal_sent", "checkout", "customer",
    "onboarded", "activated",
)

REPLY_CLASSIFICATIONS = (
    "interested", "maybe", "question", "objection", "not_interested",
    "wrong_person", "unsubscribe", "out_of_office", "existing_customer",
    "spam", "high_intent",
)


class FunnelEvent(Base):
    __tablename__ = "funnel_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prospect_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("prospects.id"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    from_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    to_stage: Mapped[str] = mapped_column(String, nullable=False)
    agent: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    proposed_by: Mapped[str] = mapped_column(String, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(String, nullable=False)
    expected_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="proposed")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeChunk(Base):
    """Semantic knowledge. embedding is TEXT (JSON-encoded float list) for now --
    pgvector isn't installed in this environment; swapping in a real VECTOR
    column is a one-file migration change, not a redesign (see docs/ARCHITECTURE.md)."""
    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Product intelligence: feedback -> structured, scored roadmap proposals
# (Product Management Agent). Real counts only -- see docs/ARCHITECTURE.md's
# rule that the Product Agent must not act on one customer's request alone;
# feedback_count on roadmap_items is that count, computed, not fabricated.
# ---------------------------------------------------------------------------

class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)         # 'customer' | 'prospect' | 'founder'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String, nullable=True)  # free-text tag, e.g. 'bug', 'feature_request'
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RoadmapItem(Base):
    __tablename__ = "roadmap_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="proposed")  # proposed|planned|in_progress|shipped|rejected
    feedback_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("product_id", "title", name="uq_roadmap_items_product_title"),)


ROADMAP_STATUSES = ("proposed", "planned", "in_progress", "shipped", "rejected")


# ---------------------------------------------------------------------------
# Operations tier: founder login, encrypted secrets, worker liveness.
#
# These tables are ADMIN-CONNECTION ONLY. The migration that creates them
# REVOKEs every privilege on them from `app_role` -- the role agents and tools
# run as -- so even a bug or injection inside agent code cannot read password
# hashes, session tokens, or secret ciphertext (proved in tests/test_ops_tables_isolation.py).
# ---------------------------------------------------------------------------

class FounderUser(Base):
    __tablename__ = "founder_users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)            # stored lower-cased
    password_hash: Mapped[str] = mapped_column(String, nullable=False)                 # scrypt, see apps/api/security.py
    totp_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)           # Fernet-encrypted; None = 2FA off
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FounderSession(Base):
    __tablename__ = "founder_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("founder_users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)       # sha256 of the bearer token; the token itself is never stored
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)


class StoredSecret(Base):
    __tablename__ = "secrets"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)                       # Fernet token, see packages/core/secrets.py
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(String, nullable=False)                          # 'worker' | 'inbound'
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")     # running | stopping | stopped
    tasks_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Acquisition engine. ADMIN-CONNECTION ONLY (revoked from app_role in the migration): these tables
# hold the safety switches and the send ledger. Agent code must not be able to flip its own
# brakes, edit the ledger that enforces the daily cap, or edit the suppression list.
# ---------------------------------------------------------------------------

class AutopilotState(Base):
    __tablename__ = "autopilot_state"

    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="approval")      # 'approval' | 'auto'
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)             # set by a safety breaker; blocks ALL sends
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    cursor: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)          # discovery rotation / exhaustion bookkeeping
    readiness: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # --- the founder's campaign goal (see packages/core/campaign.py). No default: autopilot cannot start without one. ---
    campaign_status: Mapped[str] = mapped_column(String, nullable=False, default="unset")      # unset | pending | active | completed | expired
    campaign_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campaign_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campaign_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    campaign_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    prospect_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)                           # first_contact | follow_up
    to_email: Mapped[str] = mapped_column(String, nullable=False)                       # lower-cased
    to_domain: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="reserved")     # reserved | sent | logged | failed | bounced
    approval: Mapped[str] = mapped_column(String, nullable=False, default="founder")    # founder | auto | legacy
    provider_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    bounce_type: Mapped[str | None] = mapped_column(String, nullable=True)              # hard | soft
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Suppression(Base):
    __tablename__ = "suppressions"

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)                           # email | domain
    value: Mapped[str] = mapped_column(String, nullable=False)                          # lower-cased
    reason: Mapped[str] = mapped_column(String, nullable=False)                         # unsubscribe | hard_bounce | not_interested | ...
    source: Mapped[str] = mapped_column(String, nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("company_id", "kind", "value", name="uq_suppression_company_kind_value"),)


class GrowthReport(Base):
    __tablename__ = "growth_reports"

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    period: Mapped[str] = mapped_column(String, nullable=False)                         # daily | weekly
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    bottlenecks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("product_id", "period", "period_start", name="uq_growth_report_period"),)
