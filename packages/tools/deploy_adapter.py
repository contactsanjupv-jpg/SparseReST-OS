"""
Interface the DevOps Agent's deploy tool call goes through. StubDeployAdapter
never touches real infrastructure -- it exists so the approval-gate and
audit-trail plumbing around deployment can be built and tested for real
before any real cloud/CI deploy integration is wired in.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class DeployResult:
    deployed: bool
    environment: str
    note: str


class DeployAdapter(ABC):
    @abstractmethod
    def deploy(self, *, repo: str, branch: str, environment: str) -> DeployResult:
        raise NotImplementedError


class StubDeployAdapter(DeployAdapter):
    def deploy(self, *, repo: str, branch: str, environment: str) -> DeployResult:
        return DeployResult(
            deployed=False, environment=environment,
            note=f"[STUB] would have deployed {repo}@{branch} to {environment} -- no real deploy target configured.",
        )
