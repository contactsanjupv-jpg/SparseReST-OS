"""
The interface the real lead-gen tool ("lead machine") is meant to implement
once its repository is shared and inspected -- per instructions, it is NOT
being rebuilt, and this stub is NOT a substitute for it. StubEnrichmentTool
exists solely so the Prospecting Agent's pipeline (find -> enrich -> score ->
dedup) can be built and tested for real right now, without blocking on that
repository, and without ever mislabeling fabricated data as verified.

When the real tool is wired in, it must return CompanyLead objects through
this same interface -- the Prospecting Agent's code does not change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class CompanyLead:
    company_name: str
    website: Optional[str]
    contact_name: Optional[str]
    contact_title: Optional[str]
    contact_email: Optional[str]
    contact_email_source: Optional[str]      # e.g. 'pattern_match', 'provider_x', None
    contact_email_confidence: Optional[float]  # 0.0-1.0
    contact_email_verified: bool             # True ONLY if an actual verification step ran -- never assumed


class EnrichmentTool(ABC):
    @abstractmethod
    def find_and_enrich(self, *, icp: dict, limit: int) -> list[CompanyLead]:
        """Find companies matching icp and return up to `limit` enriched leads.
        Implementations must never mark contact_email_verified=True without an
        actual verification step having run -- see docs/ARCHITECTURE.md Section 3."""
        raise NotImplementedError


class StubEnrichmentTool(EnrichmentTool):
    """Deterministic, offline, $0-cost. Every field is explicitly fake and
    explicitly marked unverified, so nothing downstream can mistake this for
    real prospect data -- if this were ever pointed at a live outreach send,
    the unverified/low-confidence flags are what stop it from being treated
    as ready to contact."""

    def find_and_enrich(self, *, icp: dict, limit: int) -> list[CompanyLead]:
        results = []
        for i in range(limit):
            results.append(CompanyLead(
                company_name=f"[STUB] Example Company {i + 1}",
                website=f"https://example-{i + 1}.invalid",
                contact_name=f"[STUB] Contact {i + 1}",
                contact_title="Owner",
                contact_email=f"stub{i + 1}@example.invalid",
                contact_email_source="stub_generator",
                contact_email_confidence=0.0,
                contact_email_verified=False,
            ))
        return results
