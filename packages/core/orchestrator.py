"""
The task-queue consumer/dispatcher. Two dispatch paths, deliberately kept
distinct:

- run_pending_tasks(): direct, single-caller dispatch (Playground 'run now',
  tests). Not concurrency-safe against multiple simultaneous callers.
- run_one_worker_cycle() / run_worker_loop(): the real, concurrency-safe
  worker path, using packages/core/queue.py's FOR UPDATE SKIP LOCKED claim.
  This is what an actual deployed background worker process runs (or several
  of them, safely, in parallel) -- see run_worker_loop's docstring.

Retries, backoff, and stuck-job recovery live in packages/core/queue.py and
packages/core/services.py::TaskService.fail_or_retry -- this module only
dispatches; it does not decide retry policy itself.
"""
from __future__ import annotations

import uuid
from typing import Callable, Optional

from packages.agents.base import BaseAgent
from packages.agents.departments.analytics import AnalyticsAgent
from packages.agents.departments.ceo import CEOAgent
from packages.agents.departments.customer_success import CustomerSuccessAgent
from packages.agents.departments.devops import DevOpsAgent
from packages.agents.departments.engineering import EngineeringAgent
from packages.agents.departments.finance import FinanceAgent
from packages.agents.departments.growth import GrowthAgent
from packages.agents.departments.legal import LegalAgent
from packages.agents.departments.market_intelligence import MarketIntelligenceAgent
from packages.agents.departments.marketing import MarketingAgent
from packages.agents.departments.operations import OperationsAgent
from packages.agents.departments.people_hr import PeopleHRAgent
from packages.agents.departments.product_management import ProductManagementAgent
from packages.agents.departments.prospecting import ProspectingAgent
from packages.agents.departments.qa import QAAgent
from packages.agents.departments.sales import SalesAgent
from packages.agents.departments.strategy import StrategyAgent
from packages.agents.llm import get_llm_client
from packages.core.context import Context
from packages.core.domains import get_domain_checker
from packages.core.services import TaskService
from packages.db.models import Agent, Task
from packages.db.session import session_scope
from packages.tools.deploy_adapter import StubDeployAdapter
from packages.tools.enrichment import StubEnrichmentTool
from packages.tools.github_adapter import StubGitHubAdapter
from packages.tools.outreach import LoggingOutreachProvider
from packages.tools.test_runner import StubTestRunner


def _build_enrichment_tool():
    from packages.core.config import get_settings
    settings = get_settings()
    if settings.enrichment_mode == "free":
        import httpx
        from packages.tools.free_enrichment import FreeEnrichmentTool
        return FreeEnrichmentTool(httpx.Client())
    return StubEnrichmentTool()


def _build_research_tool():
    from packages.core.config import get_settings
    if get_settings().enrichment_mode == "free":
        import httpx
        from packages.tools.research import WebsiteResearchTool
        return WebsiteResearchTool(httpx.Client())      # redirects are followed manually so every hop is SSRF-checked
    from packages.tools.research import StubResearchTool
    return StubResearchTool()


def _build_outreach_provider():
    from packages.core.config import get_settings
    from packages.tools.outreach import SMTPOutreachProvider
    settings = get_settings()
    if settings.outreach_mode == "smtp" and settings.smtp_host and settings.smtp_username and settings.smtp_password and settings.smtp_from_email:
        return SMTPOutreachProvider(
            host=settings.smtp_host, port=settings.smtp_port,
            username=settings.smtp_username, password=settings.smtp_password,
            from_email=settings.smtp_from_email, from_name=settings.smtp_from_name,
            daily_send_cap=settings.smtp_daily_send_cap,
        )
    return LoggingOutreachProvider()


def default_agent_registry() -> dict[str, BaseAgent]:
    """The $0-cost-by-default wiring. Which concrete tool implementation gets
    built here is controlled by packages/core/config.py's enrichment_mode/
    outreach_mode settings -- 'stub' (deterministic, no network, what every
    automated test in this repo relies on) unless explicitly switched to a
    real free option ('free' enrichment via OpenStreetMap, 'smtp' outreach
    via your own mailbox). Swapping StubGitHubAdapter for a real GitHub
    client is the same pattern, just not yet config-driven since no free
    GitHub-equivalent applies -- that one still requires your own token."""
    return {
        "ceo": CEOAgent(),
        "growth": GrowthAgent(),
        "prospecting": ProspectingAgent(_build_enrichment_tool(), research_tool=_build_research_tool(),
                                        domain_checker=get_domain_checker(), llm=get_llm_client()),
        "sales": SalesAgent(_build_outreach_provider(), llm=get_llm_client()),
        "engineering": EngineeringAgent(StubGitHubAdapter()),
        "analytics": AnalyticsAgent(),
        "customer_success": CustomerSuccessAgent(),
        "qa": QAAgent(StubTestRunner()),
        "devops": DevOpsAgent(StubDeployAdapter()),
        "finance": FinanceAgent(),
        "marketing": MarketingAgent(),
        "strategy": StrategyAgent(),
        "market_intelligence": MarketIntelligenceAgent(),
        "product_management": ProductManagementAgent(),
        "operations": OperationsAgent(),
        "people_hr": PeopleHRAgent(),
        "legal": LegalAgent(),
    }


def run_task(task_id: uuid.UUID, registry: Optional[dict[str, BaseAgent]] = None) -> dict:
    registry = registry or default_agent_registry()

    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} does not exist")
        agent_row = session.get(Agent, task.agent_id)
        if agent_row is None:
            raise ValueError(f"Task {task_id} is assigned to unknown agent_id {task.agent_id}")
        agent_slug, company_id, product_id, agent_id, goal, context_refs = (
            agent_row.slug, task.company_id, task.product_id, task.agent_id, task.goal, task.context_refs,
        )

    agent = registry.get(agent_slug)
    if agent is None:
        raise ValueError(f"No agent implementation registered for slug '{agent_slug}' (task {task_id})")

    ctx = Context(company_id=company_id, agent_id=agent_id, product_id=product_id)
    return agent.execute(ctx, task_id, goal=goal, context_refs=context_refs)


def run_pending_tasks(limit: int = 50, registry: Optional[dict[str, BaseAgent]] = None,
                       product_id: Optional[uuid.UUID] = None) -> list[dict]:
    """product_id is optional: omitted, this processes the whole company-wide
    queue (the real production shape of a background worker). Passed, it only
    processes that product's pending tasks -- useful for per-product worker
    sharding in production, and for keeping tests isolated from each other's
    leftover queue state.

    This is the direct-dispatch path (reads status='pending' without a
    concurrency-safe claim) -- fine for a single caller, e.g. the Playground's
    'run now' button or a test. For anything running as a background worker,
    or with more than one worker process, use run_one_worker_cycle /
    run_worker_loop instead, which claim tasks via FOR UPDATE SKIP LOCKED and
    are safe under concurrency."""
    with session_scope() as session:
        query = session.query(Task).filter_by(status="pending")
        if product_id is not None:
            query = query.filter_by(product_id=product_id)
        pending_ids = [t.id for t in query.order_by(Task.priority.asc(), Task.created_at.asc()).limit(limit).all()]

    results = []
    for task_id in pending_ids:
        try:
            result = run_task(task_id, registry=registry)
            results.append({"task_id": str(task_id), "status": "done", "result": result})
        except Exception as exc:
            # run_task/execute() already recorded the failure (or scheduled a retry) --
            # this catch is only so one bad task doesn't stop the rest of the batch.
            results.append({"task_id": str(task_id), "status": "error", "error": str(exc)})
    return results


def run_one_worker_cycle(worker_id: str, registry: Optional[dict[str, BaseAgent]] = None,
                          product_id: Optional[uuid.UUID] = None) -> Optional[dict]:
    """See _run_one_worker_cycle -- this wrapper only binds the worker id into every log line."""
    from packages.core.log import bind_log_context
    with bind_log_context(worker_id=worker_id):
        return _run_one_worker_cycle(worker_id, registry=registry, product_id=product_id)


def _run_one_worker_cycle(worker_id: str, registry: Optional[dict[str, BaseAgent]] = None,
                           product_id: Optional[uuid.UUID] = None) -> Optional[dict]:
    """The concurrency-safe worker path: atomically claims one task (see
    packages/core/queue.py -- FOR UPDATE SKIP LOCKED, so two workers calling
    this concurrently never claim the same task), executes it, and returns a
    result dict, or None if the queue (for this scope) is currently empty.
    A real background worker process is just this in a loop -- see
    run_worker_loop below."""
    from packages.core.queue import claim_next_task

    claimed = claim_next_task(worker_id, product_id=product_id)
    if claimed is None:
        return None

    registry = registry or default_agent_registry()
    with session_scope() as session:
        agent_row = session.get(Agent, claimed.agent_id)
        agent_slug = agent_row.slug if agent_row else None

    agent = registry.get(agent_slug)
    if agent is None:
        # Claimed a task for an agent with no registered implementation -- this is a
        # configuration bug, not a transient failure. Fail it immediately rather than
        # retrying forever against an agent that will never exist this run.
        ctx = Context(company_id=claimed.company_id, agent_id=claimed.agent_id, product_id=claimed.product_id)
        TaskService.fail_or_retry(ctx, claimed.id, reason=f"No agent implementation registered for slug '{agent_slug}'")
        return {"task_id": str(claimed.id), "status": "error", "error": f"unregistered agent '{agent_slug}'"}

    ctx = Context(company_id=claimed.company_id, agent_id=claimed.agent_id, product_id=claimed.product_id)
    try:
        result = agent.execute(ctx, claimed.id, goal=claimed.goal, context_refs=claimed.context_refs)
        return {"task_id": str(claimed.id), "status": "done", "result": result}
    except Exception as exc:
        # Already recorded (retry scheduled, or terminal failure + incident) inside
        # agent.execute() -- this catch exists only so the worker loop keeps running.
        return {"task_id": str(claimed.id), "status": "error", "error": str(exc)}


def run_worker_loop(worker_id: str, *, poll_interval_seconds: float = 1.0,
                     max_iterations: Optional[int] = None, registry: Optional[dict[str, BaseAgent]] = None,
                     product_id: Optional[uuid.UUID] = None, reap_every_seconds: float = 60.0,
                     should_stop: Optional[Callable[[], bool]] = None,
                     sleep_fn: Optional[Callable[[float], object]] = None,
                     on_cycle: Optional[Callable[[int], None]] = None) -> int:
    """A real background worker: claim-execute-repeat, sleeping when the queue
    is empty, and periodically reaping stuck tasks (packages/core/queue.py::
    requeue_stuck_tasks) so a crashed worker's lock doesn't hold a task
    hostage forever. `max_iterations` exists so this is directly testable
    (run N cycles and stop) -- omit it to run forever, which is how this
    would actually be deployed (a long-lived process, or `while True` in a
    supervised script). `product_id` shards the worker to one product's
    queue, the same way run_pending_tasks does. Returns the number of tasks
    actually processed.

    Production hooks (all optional, so the bounded test mode is unchanged): `should_stop` is
    checked before every claim -- the loop finishes the task in hand and exits, which is what
    makes SIGTERM/rolling deploys safe; `sleep_fn` lets the caller make idle sleeps
    interruptible; `on_cycle(processed)` runs every iteration (heartbeat, maintenance)."""
    import time
    from packages.core.queue import requeue_stuck_tasks

    sleep = sleep_fn or time.sleep
    processed = 0
    iterations = 0
    last_reap = time.monotonic()
    while max_iterations is None or iterations < max_iterations:
        if should_stop is not None and should_stop():
            break
        iterations += 1

        if time.monotonic() - last_reap >= reap_every_seconds:
            requeue_stuck_tasks()
            last_reap = time.monotonic()

        result = run_one_worker_cycle(worker_id, registry=registry, product_id=product_id)
        if on_cycle is not None:
            on_cycle(processed)
        if result is None:
            if max_iterations is not None:
                break  # nothing to do and we're in bounded test mode -- stop rather than idle-loop
            sleep(poll_interval_seconds)
            continue
        processed += 1
    return processed
