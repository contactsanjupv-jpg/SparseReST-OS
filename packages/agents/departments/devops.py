"""
Single entry point: deploy a branch to an environment. Staging deploys could
reasonably run autonomously in a more mature version of this system; this
MVP is deliberately conservative and requires approval for every environment,
because there is no environment-tiering config yet to safely distinguish
"low-risk staging" from "high-risk production" -- see docs/ARCHITECTURE.md
Section 8's explicit rule that production-destructive changes require
approval, and STATUS.md for the staging/production distinction as a
documented follow-up, not a silent gap.
"""
from __future__ import annotations

from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.core.services import ApprovalService, EventService
from packages.tools.deploy_adapter import DeployAdapter
from packages.tools.execution import call_tool


class DevOpsAgent(BaseAgent):
    slug = "devops"

    def __init__(self, deploy_adapter: DeployAdapter):
        self._deploy = deploy_adapter

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        context_refs = context_refs or {}
        repo = context_refs["repo"]
        branch = context_refs["branch"]
        environment = context_refs.get("environment", "production")

        approval_id = ApprovalService.request(
            task_id=ctx.task_id, requested_by=self.slug, risk_level="high",
            summary=f"Deploy {repo}@{branch} to {environment}",
        )
        EventService.emit(ctx, "devops.deploy_requested", {"repo": repo, "branch": branch, "environment": environment, "approval_id": str(approval_id)})

        if not context_refs.get("approved", False):
            return {"status": "needs_approval", "approval_id": str(approval_id)}

        result = call_tool(
            ctx, "deploy", self._deploy.deploy,
            input={"repo": repo, "branch": branch, "environment": environment},
            repo=repo, branch=branch, environment=environment,
        )
        EventService.emit(ctx, "devops.deployed", {"repo": repo, "environment": environment, "deployed": result.deployed})
        return {"status": "done", "deployed": result.deployed, "note": result.note}
