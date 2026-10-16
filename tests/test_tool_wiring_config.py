"""
Proves the config-driven tool wiring itself: default (unset) mode uses the
stub tools every other test relies on; explicitly switching the mode swaps
in the real implementation, without touching ProspectingAgent/SalesAgent
code at all.
"""
from __future__ import annotations

from packages.agents.departments.prospecting import ProspectingAgent
from packages.agents.departments.sales import SalesAgent
from packages.core.config import get_settings
from packages.core.orchestrator import default_agent_registry
from packages.tools.enrichment import StubEnrichmentTool
from packages.tools.free_enrichment import FreeEnrichmentTool
from packages.tools.outreach import LoggingOutreachProvider, SMTPOutreachProvider


def test_default_mode_is_stub_everywhere():
    get_settings.cache_clear()
    registry = default_agent_registry()
    prospecting = registry["prospecting"]
    sales = registry["sales"]
    assert isinstance(prospecting, ProspectingAgent)
    assert isinstance(prospecting._enrichment_tool, StubEnrichmentTool)
    assert isinstance(sales, SalesAgent)
    assert isinstance(sales._outreach, LoggingOutreachProvider)


def test_enrichment_mode_free_switches_to_the_real_tool(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_MODE", "free")
    get_settings.cache_clear()
    try:
        registry = default_agent_registry()
        assert isinstance(registry["prospecting"]._enrichment_tool, FreeEnrichmentTool)
    finally:
        monkeypatch.delenv("ENRICHMENT_MODE", raising=False)
        get_settings.cache_clear()


def test_outreach_mode_smtp_switches_only_when_fully_configured(monkeypatch):
    monkeypatch.setenv("OUTREACH_MODE", "smtp")
    get_settings.cache_clear()
    try:
        # Incomplete SMTP config (no host/user/password/from set) -- must NOT
        # silently construct a broken SMTPOutreachProvider; falls back to logging.
        registry = default_agent_registry()
        assert isinstance(registry["sales"]._outreach, LoggingOutreachProvider)

        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_USERNAME", "sales@sparserest.com")
        monkeypatch.setenv("SMTP_PASSWORD", "app-password")
        monkeypatch.setenv("SMTP_FROM_EMAIL", "sales@sparserest.com")
        get_settings.cache_clear()
        registry = default_agent_registry()
        assert isinstance(registry["sales"]._outreach, SMTPOutreachProvider)
    finally:
        for var in ("OUTREACH_MODE", "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL"):
            monkeypatch.delenv(var, raising=False)
        get_settings.cache_clear()
