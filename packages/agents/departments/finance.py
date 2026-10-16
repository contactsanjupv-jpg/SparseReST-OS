"""
Company-level. Its one grounded action right now is "what has the AI
workforce actually cost so far" -- computed from real audit_log.cost_usd
rows, which is data this system already has, rather than fabricating a
revenue/profitability picture that would need `customers`/`revenue_events`
tables not yet built (see AnalyticsAgent's same honesty rule). This directly
serves the $0-cost-first principle: it's the actual mechanism for answering
"are we staying near $0" rather than trusting that we are.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.db.models import AuditLog, Product
from packages.db.scoped_session import scoped_session


class FinanceAgent(BaseAgent):
    slug = "finance"

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        if ctx.product_id is not None:
            raise ValueError("FinanceAgent is company-level -- it should never be invoked with a specific product_id in context")

        with scoped_session(ctx) as session:
            products = session.query(Product).filter_by(company_id=ctx.company_id).all()
            product_names = {p.id: p.slug for p in products}
            audit_rows = session.query(AuditLog).filter_by(company_id=ctx.company_id).all()

            cost_by_product: dict = defaultdict(float)
            cost_by_tool: dict = defaultdict(float)
            for row in audit_rows:
                cost = float(row.cost_usd or 0)
                cost_by_product[product_names.get(row.product_id, "company-level")] += cost
                cost_by_tool[row.tool] += cost

        total_cost = sum(cost_by_product.values())
        return {
            "status": "done",
            "total_cost_usd": round(total_cost, 4),
            "cost_by_product": {k: round(v, 4) for k, v in cost_by_product.items()},
            "cost_by_tool": {k: round(v, 4) for k, v in cost_by_tool.items()},
            "note": (
                "Cost only -- revenue/MRR/profitability requires customers/revenue_events "
                "tables not yet built (see AnalyticsAgent), so this deliberately does not "
                "report them rather than fabricating a number."
            ),
        }
