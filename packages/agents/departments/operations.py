"""
Company-level. Two entry points via context_refs['action']:

- 'record_sop': writes an SOP into knowledge_chunks (category='sop'),
  optionally scoped to one product or company-wide (product_id=None).
- 'list_sops': reads them back, optionally filtered by product.

No fabricated process metrics -- this is real storage/retrieval over data
the founder actually provides, nothing invented.
"""
from __future__ import annotations

from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.core.services import EventService
from packages.db.models import KnowledgeChunk
from packages.db.session import session_scope


class OperationsAgent(BaseAgent):
    slug = "operations"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        context_refs = context_refs or {}
        action = context_refs.get("action")
        if action == "record_sop":
            return self._record_sop(ctx, context_refs)
        if action == "list_sops":
            return self._list_sops(ctx, context_refs)
        raise ValueError(f"OperationsAgent received unknown action: {action!r}")

    def _record_sop(self, ctx: Context, context_refs: dict) -> dict:
        content = context_refs["content"]
        product_id = context_refs.get("product_id")  # None = company-wide SOP

        # knowledge_chunks isn't RLS-scoped by company_id (it's keyed by product_id,
        # nullable = company-wide) -- see the RLS migration's coverage note. Using the
        # admin connection here is consistent with that, not a bypass of anything RLS
        # actually enforces for this table.
        with session_scope() as session:
            chunk = KnowledgeChunk(product_id=product_id, category="sop", content=content, source=f"recorded by {ctx.agent_id}")
            session.add(chunk)
            session.flush()
            chunk_id = chunk.id

        EventService.emit(ctx, "operations.sop_recorded", {"chunk_id": str(chunk_id), "product_id": str(product_id) if product_id else None})
        return {"status": "done", "sop_id": str(chunk_id)}

    def _list_sops(self, ctx: Context, context_refs: dict) -> dict:
        product_id = context_refs.get("product_id")
        with session_scope() as session:
            query = session.query(KnowledgeChunk).filter_by(category="sop")
            query = query.filter_by(product_id=product_id) if product_id else query.filter(KnowledgeChunk.product_id.is_(None))
            sops = query.order_by(KnowledgeChunk.created_at.desc()).all()
            out = [{"id": str(s.id), "content": s.content, "source": s.source} for s in sops]
        return {"status": "done", "sops": out, "count": len(out)}
