"""
Every tool call an agent makes goes through `call_tool`, never invoked
directly. This is what makes "every important agent action has an audit
trail" and "least-privilege tool access" true by construction:

- Permission check: is this agent allowed to use this tool at all, and (for
  product-level agents) is the tool enabled for the current product via
  product_agent_enablement? Denied calls are audited too (result='denied'),
  not silently dropped.
- Execution: the tool itself never talks to audit_log/activity_events --
  that's this wrapper's job, so a tool can't forget to log itself.
- Failure handling: an exception from the tool is caught, logged as
  result='error', and re-raised -- callers still see the failure (so retry/
  fail logic upstream works), but it's never lost from the audit trail.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from packages.core.context import Context
from packages.core.services import AuditService
from packages.db.models import Agent, ProductAgentEnablement
from packages.db.session import session_scope

# Tools every agent may use regardless of department (read-only, low-risk).
COMMON_TOOLS = {"web_research"}

# Which department each tool belongs to -- an agent may only call tools for
# its own department plus COMMON_TOOLS. This is deliberately simple (no
# per-tool ACL table yet) and documented as a Phase-2 simplification in
# docs/ARCHITECTURE.md; least-privilege is still enforced, just at
# department granularity rather than per-agent granularity for now.
DEPARTMENT_TOOLS = {
    "growth": {"enrichment", "research", "outreach_send", "funnel_query"},
    "engineering": {"github", "test_runner", "deploy"},
}


def _agent_department(agent_id) -> tuple[str, bool]:
    with session_scope() as session:
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise ValueError(f"Unknown agent_id {agent_id}")
        return agent.department, agent.is_company_level


def _tool_enabled_for_product(product_id, agent_id) -> bool:
    if product_id is None:
        return True  # company-level context has no product-scoped enablement to check
    with session_scope() as session:
        row = session.query(ProductAgentEnablement).filter_by(product_id=product_id, agent_id=agent_id).one_or_none()
        return bool(row and row.enabled)


class ToolPermissionError(PermissionError):
    pass


def call_tool(ctx: Context, tool_name: str, fn: Callable[..., Any], *, input: Optional[dict] = None, cost_usd: float = 0, **kwargs) -> Any:
    department, is_company_level = _agent_department(ctx.agent_id)
    allowed = tool_name in COMMON_TOOLS or tool_name in DEPARTMENT_TOOLS.get(department, set())

    if not allowed:
        AuditService.record(ctx, tool=tool_name, input=input, output=None, result="denied")
        raise ToolPermissionError(f"Agent in department '{department}' is not permitted to use tool '{tool_name}'")

    if not is_company_level and not _tool_enabled_for_product(ctx.product_id, ctx.agent_id):
        AuditService.record(ctx, tool=tool_name, input=input, output=None, result="denied")
        raise ToolPermissionError(f"Agent is not enabled for product {ctx.product_id} -- check product_agent_enablement")

    try:
        output = fn(**kwargs)
        AuditService.record(ctx, tool=tool_name, input=input, output=_safe_output(output), result="success", cost_usd=cost_usd)
        return output
    except Exception as exc:
        AuditService.record(ctx, tool=tool_name, input=input, output={"error": str(exc)}, result="error", cost_usd=cost_usd)
        raise


def _safe_output(output: Any) -> Optional[dict]:
    """audit_log.output is JSONB -- never persist something that isn't JSON-serializable
    as a silent failure; coerce dataclasses/lists to plain dicts, or log a shape note if
    we truly can't represent it."""
    import dataclasses

    def coerce(value: Any) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return dataclasses.asdict(value)
        if isinstance(value, list):
            return [coerce(v) for v in value]
        if isinstance(value, dict):
            return {k: coerce(v) for k, v in value.items()}
        return value

    if output is None:
        return None
    coerced = coerce(output)
    if isinstance(coerced, dict):
        return coerced
    if isinstance(coerced, list):
        return {"items": coerced}
    return {"value": str(coerced)}
