"""
The context object every unit of work carries.

This is the concrete implementation of the "no agent ever has an ambiguous
current product" requirement: company_id and agent_id are always required;
product_id is required for product-scoped work and *must* be None for
genuine company-level work (never an unset/forgotten value); task_id and
workflow_id are attached once those exist.

Constructing a Context with a bad combination should fail loudly, in code,
not be caught later by a human reading logs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID


@dataclass(frozen=True)
class Context:
    company_id: UUID
    agent_id: UUID
    product_id: Optional[UUID] = None
    task_id: Optional[UUID] = None
    workflow_id: Optional[UUID] = None

    def require_product(self) -> UUID:
        """Call this from any product-level agent/tool. Fails loudly instead of
        silently operating with an ambiguous product scope."""
        if self.product_id is None:
            raise ValueError(
                "This action requires a product_id, but the context is company-scoped "
                "(product_id=None). A product-level agent received a company-level task "
                "-- that's a bug in task assignment, not a valid state to work around."
            )
        return self.product_id

    def with_task(self, task_id: UUID, workflow_id: Optional[UUID] = None) -> "Context":
        return Context(
            company_id=self.company_id,
            agent_id=self.agent_id,
            product_id=self.product_id,
            task_id=task_id,
            workflow_id=workflow_id or self.workflow_id,
        )
