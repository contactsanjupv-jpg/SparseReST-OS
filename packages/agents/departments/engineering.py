"""
Two entry points, dispatched via context_refs['action'], mirroring the
Customer request -> Engineering plan -> approval -> branch/PR flow in
docs/ARCHITECTURE.md:

- 'plan': reads the issue (via GitHubAdapter) and produces an implementation
  plan. Read-only -- no approval needed.
- 'open_pr': approval-gated (opening a branch/PR is a real, if currently
  stubbed, external action) -- creates a branch and opens a PR only once
  approved.

StubGitHubAdapter is the default -- nothing here touches a real repository
until a real GitHubAdapter implementation is wired into the registry.
"""
from __future__ import annotations

from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.core.services import ApprovalService, EventService
from packages.tools.execution import call_tool
from packages.tools.github_adapter import GitHubAdapter


class EngineeringAgent(BaseAgent):
    slug = "engineering"

    def __init__(self, github: GitHubAdapter):
        self._github = github

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        context_refs = context_refs or {}
        action = context_refs.get("action")
        if action == "plan":
            return self._plan(ctx, context_refs)
        if action == "open_pr":
            return self._open_pr(ctx, context_refs)
        raise ValueError(f"EngineeringAgent received unknown action: {action!r}")

    def _plan(self, ctx: Context, context_refs: dict) -> dict:
        repo = context_refs["repo"]
        issue_number = context_refs["issue_number"]

        issue = call_tool(
            ctx, "github", self._github.get_issue,
            input={"repo": repo, "issue_number": issue_number}, repo=repo, issue_number=issue_number,
        )

        # Deterministic plan skeleton -- swapping in real LLM-based planning
        # (packages/agents/llm.py, model_tier='default') is the natural next
        # step once ANTHROPIC_API_KEY is live; the plan's *shape* (steps,
        # referencing the real issue) doesn't change when that happens.
        plan = {
            "issue_title": issue.title,
            "steps": [
                "Reproduce the issue locally",
                "Identify the minimal code change that resolves it",
                "Write/update a test that fails before the fix and passes after",
                "Implement the fix",
                "Run the full test suite",
                "Open a PR for review",
            ],
        }
        EventService.emit(ctx, "engineering.plan_created", {"repo": repo, "issue_number": issue_number})
        return {"status": "done", "plan": plan}

    def _open_pr(self, ctx: Context, context_refs: dict) -> dict:
        repo = context_refs["repo"]
        branch_name = context_refs["branch_name"]
        title = context_refs.get("title", f"Fix for {repo}")

        approval_id = ApprovalService.request(
            task_id=ctx.task_id, requested_by=self.slug, risk_level="medium",
            summary=f"Open a PR on {repo}: branch '{branch_name}' -- {title}",
        )
        EventService.emit(ctx, "engineering.pr_prepared", {"repo": repo, "approval_id": str(approval_id)})

        if not context_refs.get("approved", False):
            return {"status": "needs_approval", "approval_id": str(approval_id)}

        call_tool(
            ctx, "github", self._github.create_branch,
            input={"repo": repo, "branch": branch_name}, repo=repo, base_branch="main", new_branch=branch_name,
        )
        pr = call_tool(
            ctx, "github", self._github.open_pull_request,
            input={"repo": repo, "branch": branch_name}, repo=repo, branch=branch_name, base_branch="main",
            title=title, body="Opened by the Engineering Agent.",
        )
        EventService.emit(ctx, "engineering.pr_opened", {"repo": repo, "pr_url": pr.url})
        return {"status": "done", "pr_url": pr.url, "branch": pr.branch}
