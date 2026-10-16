"""
Interface the Engineering Agent's GitHub tool calls go through. StubGitHubAdapter
is the $0-cost, no-credentials default -- deterministic fake responses, so the
Engineering Agent's read/plan/branch/PR flow can be built and tested for real
right now, without a GitHub token and without touching any real repository.

A real implementation (e.g. wrapping PyGithub or the GitHub REST API directly)
plugs in behind this same interface -- see packages/agents/departments/engineering.py,
which never changes when that happens. Per instructions, existing working
tools are not being rebuilt; this is scaffolding for wiring one in, not a
replacement for one.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)


@dataclass
class PullRequest:
    number: int
    url: str
    branch: str
    title: str


class GitHubAdapter(ABC):
    @abstractmethod
    def get_issue(self, *, repo: str, issue_number: int) -> Issue:
        raise NotImplementedError

    @abstractmethod
    def create_branch(self, *, repo: str, base_branch: str, new_branch: str) -> str:
        """Returns the new branch name."""
        raise NotImplementedError

    @abstractmethod
    def open_pull_request(self, *, repo: str, branch: str, base_branch: str, title: str, body: str) -> PullRequest:
        raise NotImplementedError


class StubGitHubAdapter(GitHubAdapter):
    """Never touches a real repository. Every method returns clearly-labeled
    fake data, so nothing downstream can mistake this for a real GitHub
    interaction -- exactly the same safety property StubEnrichmentTool and
    LoggingOutreachProvider have."""

    def get_issue(self, *, repo: str, issue_number: int) -> Issue:
        return Issue(
            number=issue_number,
            title=f"[STUB] Issue #{issue_number} in {repo}",
            body="[STUB] No real GitHub connection configured -- this is fake issue data.",
            labels=["stub"],
        )

    def create_branch(self, *, repo: str, base_branch: str, new_branch: str) -> str:
        return f"[STUB] {new_branch} (not actually created in {repo})"

    def open_pull_request(self, *, repo: str, branch: str, base_branch: str, title: str, body: str) -> PullRequest:
        return PullRequest(number=0, url=f"[STUB] no real PR opened for {repo}", branch=branch, title=title)
