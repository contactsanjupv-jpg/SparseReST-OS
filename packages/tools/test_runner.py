"""
Interface the QA Agent's test-execution tool call goes through.
StubTestRunner is the $0-cost default -- deterministic fake results, so the
QA Agent's "run tests against this PR" flow can be built and tested for real
without a real CI system connected. A real implementation (invoking pytest/CI
via subprocess or a CI provider's API) plugs in behind this same interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class TestRunResult:
    passed: bool
    total: int
    failed: int
    failures: list[str] = field(default_factory=list)


class TestRunner(ABC):
    @abstractmethod
    def run(self, *, repo: str, branch: str) -> TestRunResult:
        raise NotImplementedError


class StubTestRunner(TestRunner):
    """Never runs real tests. Always reports a clearly-labeled fake passing
    result -- QA Agent code must not treat this as evidence of real quality;
    it exists only to exercise the real approval/audit/event plumbing around
    test execution before a real CI integration is wired in."""

    def run(self, *, repo: str, branch: str) -> TestRunResult:
        return TestRunResult(passed=True, total=1, failed=0, failures=[])


class StubFailingTestRunner(TestRunner):
    """A second stub, deliberately reporting failure -- lets tests and manual
    exploration exercise the QA Agent's failure path without needing a real
    broken build."""

    def run(self, *, repo: str, branch: str) -> TestRunResult:
        return TestRunResult(passed=False, total=3, failed=1, failures=["[STUB] test_example_that_fails"])
