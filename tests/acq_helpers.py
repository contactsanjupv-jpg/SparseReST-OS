"""Fixtures for the acquisition-engine tests: fake discovery/research/mail, a controllable clock, a ready-to-run product."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from packages.agents.departments.growth import GrowthAgent
from packages.agents.departments.prospecting import ProspectingAgent
from packages.agents.departments.sales import SalesAgent
from packages.agents.llm import FakeLLMClient
from packages.core import autopilot, outbound
from packages.core.domains import StaticDomainChecker
from packages.core.orchestrator import default_agent_registry, run_one_worker_cycle
from packages.db.models import Agent, AutopilotState, ProductAgentEnablement, ProductConfig, Task
from packages.db.session import session_scope
from packages.tools.enrichment import CompanyLead, EnrichmentTool
from packages.tools.outreach import OutreachProvider, SendResult
from packages.tools.research import ResearchTool
from tests.helpers import company_with_products

ICP = {
    "industries": ["plumbing"], "geography": "Testville", "buyer_titles": ["Owner"],
    "qualification": {"min_score": 0.5, "required_keywords_any": ["plumb"], "allow_role_emails": True},
}
POSITIONING = {
    "product_name": "XFlow", "one_liner": "XFlow tracks every plumbing lead from the first call to the booked job, automatically.",
    "call_to_action": "Open to a quick 15-minute call this week?", "website_url": "xflow.example",
}
SALES = {"physical_address": "100 Test Street, Testville, TS 00000", "sender_name": "Sam from XFlow"}


def lead(i: int, *, email=None, title="Owner", website=True, name=None) -> CompanyLead:
    name = name or f"Acme Plumbing {i}"
    domain = f"acme-plumbing-{i}.com"
    return CompanyLead(company_name=name, website=f"https://{domain}" if website else None, contact_name=f"Pat {i}", contact_title=title,
                       contact_email=email if email is not None else f"pat{i}@{domain}", contact_email_source="test", contact_email_confidence=0.9,
                       contact_email_verified=False)


class FakeEnrichment(EnrichmentTool):
    def __init__(self, leads=None):
        self.leads, self.calls = list(leads or []), []

    def find_and_enrich(self, *, icp, limit):
        self.calls.append({"icp": icp, "limit": limit})
        out, self.leads = self.leads[:limit], self.leads[limit:]
        return out


class FakeResearch(ResearchTool):
    def __init__(self, pages=None, default="ok"):
        self.pages, self.default, self.calls = pages or {}, default, []

    def research(self, website):
        self.calls.append(website)
        if website in self.pages:
            return dict(self.pages[website])
        if self.default == "ok":
            return good_research(website or "")
        return {"status": "unreachable", "note": "test", "fetched_at": "now"}


def good_research(website: str) -> dict:
    return {"status": "ok", "title": "Acme Plumbing", "description": "Family plumbing company serving Testville homeowners since 1998.",
            "services": ["Drain cleaning", "Water heaters"], "headings": ["Plumbing you can trust"],
            "text": "acme plumbing family plumbing company serving testville homeowners since 1998. drain cleaning and water heater repair. " * 4,
            "hook": "Family plumbing company serving Testville homeowners since 1998.", "hook_source": "meta_description",
            "hook_evidence": "Family plumbing company serving Testville homeowners since 1998.", "fetched_at": "now", "llm_used": False}


class RecordingProvider(OutreachProvider):
    """Records every send; can be told to fail with a specific exception for the next N calls."""

    def __init__(self, sent=True):
        self.messages, self.sent_flag, self.fail_with, self.fail_times = [], sent, None, 0

    def send(self, *, to_email, subject, body):
        if self.fail_with is not None and self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with
        self.messages.append({"to": to_email, "subject": subject, "body": body})
        return SendResult(sent=self.sent_flag, provider_message_id=f"<{uuid.uuid4().hex}@test>", note="recorded")


class Clock:
    """Time travel for tests WITHOUT clock skew. `now` is always the real current time (so it agrees with every
    database-generated timestamp); `advance` instead AGES the stored data -- every timestamp that records when
    something happened moves back by the delta, and deferred tasks are released -- which is exactly what the
    passage of time looks like to the code."""

    @property
    def now(self):
        return datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        from sqlalchemy import text
        secs = timedelta(**kw).total_seconds()
        aging = [("tasks", "created_at"), ("outbound_messages", "reserved_at"), ("outbound_messages", "sent_at"),
                 ("outbound_messages", "bounced_at"), ("funnel_events", "created_at"), ("approvals", "created_at"),
                 ("incidents", "created_at"), ("prospects", "created_at"), ("prospects", "updated_at"), ("prospects", "replied_at")]
        with session_scope() as session:
            for table, column in aging:
                session.execute(text(f"UPDATE {table} SET {column} = {column} - make_interval(secs => :s) WHERE {column} IS NOT NULL"), {"s": secs})
            session.execute(text("UPDATE autopilot_state SET campaign_started_at = campaign_started_at - make_interval(secs => :s) WHERE campaign_started_at IS NOT NULL"), {"s": secs})
            session.execute(text("UPDATE tasks SET next_attempt_at = now() - interval '1 second' WHERE next_attempt_at IS NOT NULL AND next_attempt_at > now() - interval '1 second'"))
            for state in session.query(AutopilotState).all():
                cursor = dict(state.cursor or {})
                for key in ("last_discovery_at", "last_followup_run"):
                    if cursor.get(key):
                        cursor[key] = (datetime.fromisoformat(cursor[key]) - timedelta(seconds=secs)).isoformat()
                state.cursor = cursor


def monday_10am_utc() -> datetime:
    d = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)       # a Monday
    assert d.weekday() == 0
    return d


def make_registry(enrichment=None, research=None, provider=None, llm=None, checker=None) -> dict:
    registry = default_agent_registry()
    llm = llm or FakeLLMClient()
    registry["prospecting"] = ProspectingAgent(enrichment or FakeEnrichment(), research_tool=research or FakeResearch(),
                                                domain_checker=checker or StaticDomainChecker(), llm=llm)
    registry["sales"] = SalesAgent(provider or RecordingProvider(), llm=llm)
    registry["growth"] = GrowthAgent()
    return registry


def drain(registry, product_id, max_cycles=200) -> int:
    """Run the real worker cycle until the queue has nothing runnable."""
    n = 0
    for _ in range(max_cycles):
        if run_one_worker_cycle("test-worker", registry=registry, product_id=product_id) is None:
            break
        n += 1
    return n


@contextmanager
def acquisition_product(*, icp=None, positioning=None, sales=None, config: dict | None = None, enable=True, clock: Clock | None = None, monkeypatch=None,
                        goal: tuple | None = (10, 30)):
    """A company+product with a complete acquisition config, agents enabled, autopilot configured (open all day,
    every day, short gap) and -- if enable -- switched on in approval mode (dry-run or live depending on settings)."""
    with company_with_products(lifecycle="active") as ids:
        pid, cid = ids["product_id"], ids["company_id"]
        with session_scope() as session:
            cfg = session.get(ProductConfig, pid)
            cfg.icp = icp if icp is not None else ICP
            cfg.positioning = positioning if positioning is not None else POSITIONING
            cfg.sales = sales if sales is not None else SALES
            for slug in ("growth", "prospecting", "sales"):
                agent = session.query(Agent).filter_by(slug=slug).one()
                session.add(ProductAgentEnablement(product_id=pid, agent_id=agent.id, enabled=True))
        base = {"start_hour": 0, "end_hour": 24, "send_days": [0, 1, 2, 3, 4, 5, 6], "warmup": False, "min_gap_seconds": 60,
                "jitter": 0.0, "domain_cooldown_days": 30, "discovery_cooldown_hours": 0.0}
        base.update(config or {})
        autopilot.update_config(cid, pid, base)
        if goal is not None:                                    # a founder-supplied goal, as in real use; goal=None leaves it unset
            from packages.core import campaign
            campaign.set_goal(pid, *goal)
        if enable:
            autopilot.enable(cid, pid, mode="approval")
        yield {**ids, "company_id": cid, "product_id": pid}
