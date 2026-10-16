"""
Goal decomposition. This is deliberately simple pattern-matching for the MVP
-- not because semantic understanding doesn't matter, but because a $0-cost,
credential-free runtime needs a deterministic fallback, and every goal shape
handled here is exactly the one the mandatory revenue-cycle test exercises
end to end. Swapping in real LLM-based decomposition (via packages/agents/llm.py,
model_tier='strong') for open-ended goals is the natural next step once a
live ANTHROPIC_API_KEY is available -- the dispatch contract (goal -> one or
more Task rows, each with the right agent_id/product_id) doesn't change.

The CEO never executes department work itself -- it only ever creates Task
rows. Who actually does the work is packages/core/orchestrator.py.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.core.services import TaskService
from packages.db.models import Agent, Product
from packages.db.session import session_scope

_FIND_PROSPECTS_RE = re.compile(r"find (\d+) .*prospects? for (\S+)", re.IGNORECASE)
_DIAGNOSE_FUNNEL_RE = re.compile(r"(diagnose|check|monitor) .*funnel for (\S+)", re.IGNORECASE)


def _resolve(session, company_id, product_slug: str) -> Product:
    product = session.query(Product).filter_by(company_id=company_id, slug=product_slug.lower()).one_or_none()
    if product is None:
        raise ValueError(f"No product with slug '{product_slug}' -- known products must exist in the registry first.")
    return product


def _agent_id_by_slug(session, slug: str) -> uuid.UUID:
    agent = session.query(Agent).filter_by(slug=slug).one_or_none()
    if agent is None:
        raise ValueError(f"No agent registered with slug '{slug}'")
    return agent.id


class CEOAgent(BaseAgent):
    slug = "ceo"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        created_task_ids: list[str] = []

        match = _FIND_PROSPECTS_RE.search(goal)
        if match:
            limit, product_slug = int(match.group(1)), match.group(2)
            with session_scope() as session:
                product_id = _resolve(session, ctx.company_id, product_slug).id
                prospecting_agent_id = _agent_id_by_slug(session, "prospecting")
            sub_ctx = Context(company_id=ctx.company_id, agent_id=prospecting_agent_id, product_id=product_id)
            task_id = TaskService.create(
                sub_ctx, goal=f"Find and qualify {limit} prospects for {product_slug}",
                created_by="ceo", context_refs={"limit": limit},
            )
            created_task_ids.append(str(task_id))
            return {"status": "done", "decomposition": "find_prospects", "created_tasks": created_task_ids}

        match = _DIAGNOSE_FUNNEL_RE.search(goal)
        if match:
            product_slug = match.group(2)
            with session_scope() as session:
                product_id = _resolve(session, ctx.company_id, product_slug).id
                growth_agent_id = _agent_id_by_slug(session, "growth")
            sub_ctx = Context(company_id=ctx.company_id, agent_id=growth_agent_id, product_id=product_id)
            task_id = TaskService.create(sub_ctx, goal=f"Diagnose funnel for {product_slug}", created_by="ceo")
            created_task_ids.append(str(task_id))
            return {"status": "done", "decomposition": "diagnose_funnel", "created_tasks": created_task_ids}

        return {
            "status": "unrecognized_goal",
            "note": (
                "This MVP CEO only recognizes 'find N prospects for <product-slug>' and "
                "'diagnose funnel for <product-slug>' goal shapes. Configure ANTHROPIC_API_KEY "
                "for open-ended goal decomposition (not yet wired in)."
            ),
        }
