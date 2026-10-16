"""
Single entry point: run tests for a given repo/branch via the pluggable
TestRunner (packages/tools/test_runner.py). Never marks a PR ready based on
its own judgment of the code -- it reports exactly what the test runner
reported, pass or fail, and nothing else decides whether that's good enough
(that's a human/CEO decision downstream, via the approval gate on
Engineering's PR-merge step, not built yet).
"""
from __future__ import annotations

from typing import Optional

from packages.agents.base import BaseAgent
from packages.core.context import Context
from packages.core.services import EventService
from packages.tools.execution import call_tool
from packages.tools.test_runner import TestRunner


class QAAgent(BaseAgent):
    slug = "qa"

    def __init__(self, test_runner: TestRunner):
        self._runner = test_runner

    def handle(self, ctx: Context, *, goal: str, context_refs: Optional[dict]) -> dict:
        context_refs = context_refs or {}
        repo = context_refs["repo"]
        branch = context_refs["branch"]

        result = call_tool(
            ctx, "test_runner", self._runner.run,
            input={"repo": repo, "branch": branch}, repo=repo, branch=branch,
        )

        EventService.emit(ctx, "qa.tests_run", {"repo": repo, "branch": branch, "passed": result.passed, "failed": result.failed})
        return {
            "status": "done",
            "passed": result.passed,
            "total": result.total,
            "failed": result.failed,
            "failures": result.failures,
        }
