"""
Centralized configuration.

One source of truth for every environment variable the system reads. Every
setting here must appear in docs/ENVIRONMENT.md with a description and
whether it's required or optional -- that cross-check is part of the CI
config-consistency check (see tests/test_environment_docs.py).

Nothing in this codebase reads os.environ directly outside this module.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # SPARSEREST_ENV_FILE lets a caller point at a different env file -- or at none (an empty value). The test
    # harness sets it to "" so a developer's real .env can never leak into the suite (see tests/conftest.py).
    model_config = SettingsConfigDict(env_file=(os.environ.get("SPARSEREST_ENV_FILE", ".env") or None), env_file_encoding="utf-8", extra="ignore")

    # --- Database (required) ---
    # Admin/migration connection -- used by Alembic and the seed script only.
    # Postgres never applies RLS to a table's owner or a superuser, so this
    # connection must NEVER be used for agent/tool runtime queries.
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/sparserest_dev"

    # The least-privileged, RLS-enforced connection every agent/tool actually
    # runs queries through (see packages/db/scoped_session.py). Change the
    # password before any non-local deployment -- 'change_me_in_env' is the
    # placeholder the migration creates the role with.
    app_database_url: str = "postgresql+psycopg://app_role:change_me_in_env@localhost:5432/sparserest_dev"

    # --- Anthropic / agent reasoning (optional in dev; required for real agent reasoning) ---
    # When unset, the agent runtime falls back to FakeLLMClient (deterministic, no network
    # calls) so the runtime, permissions, and tests all work with $0 cost and no credentials.
    anthropic_api_key: Optional[str] = None
    anthropic_model_default: str = "claude-sonnet-5"              # used for most agent reasoning
    anthropic_model_cheap: str = "claude-haiku-4-5-20251001"      # used for classification/high-volume work
    anthropic_model_strong: str = "claude-opus-5"                 # used for CEO-level synthesis
    # Model lineups move fast -- check https://docs.claude.com for current model strings/pricing
    # before relying on these defaults in production.

    # --- Outreach provider (optional in dev; required for real sends) ---
    # When unset, the Sales Agent's send tool uses LoggingOutreachProvider, which records
    # what *would* have been sent to activity_events/audit_log without contacting anyone.
    outreach_provider_api_key: Optional[str] = None
    outreach_provider_webhook_secret: Optional[str] = None

    # --- Tool wiring: which implementation default_agent_registry() actually builds ---
    # Defaults to 'stub' everywhere -- deterministic, $0-cost, no network calls, which is
    # also what every automated test in this repo relies on. Flip to a real mode only when
    # you're ready to run this for real (see docs/ENVIRONMENT.md).
    enrichment_mode: str = "stub"     # 'stub' | 'free' (OpenStreetMap-based, packages/tools/free_enrichment.py)
    outreach_mode: str = "stub"       # 'stub' | 'smtp' (packages/tools/outreach.py::SMTPOutreachProvider)

    # SMTP settings -- only read when outreach_mode='smtp'. Use a mailbox you already own
    # (your domain's own email account) -- this is not a new paid service.
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_email: Optional[str] = None
    smtp_from_name: str = ""
    smtp_daily_send_cap: int = 30

    # IMAP settings -- only read when inbound checking is actually run (see
    # packages/core/inbound.py). Defaults to the same mailbox as SMTP, since
    # that's the common case (one mailbox, sends and receives both).
    imap_host: Optional[str] = None
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None

    # --- Notifications (optional in dev; the console stub is used when unset) ---
    slack_webhook_url: Optional[str] = None

    # --- API auth (optional in dev -- unset means the API is open, localhost-only use) ---
    founder_api_key: Optional[str] = None

    # --- App ---
    environment: str = "development"
    log_level: str = "INFO"
    log_format: str = "auto"          # 'auto' (json in production, console otherwise) | 'json' | 'console'

    # --- API hardening (see docs/SECURITY.md) ---
    allowed_origins: str = "http://localhost:3000"   # comma-separated CORS origins; set to your real frontend origin(s) in production
    trusted_hosts: str = "*"                        # comma-separated Host headers the API will answer to; '*' = any (dev only)
    max_request_body_bytes: int = 1_000_000
    rate_limit_per_minute: int = 120                 # per client, per API process (see apps/api/ratelimit.py)
    rate_limit_enabled: bool = True
    login_rate_limit_per_minute: int = 10             # per client IP, POST /auth/login only
    trust_proxy_headers: bool = False                 # honour X-Forwarded-For (last entry) -- only behind YOUR reverse proxy

    # --- Founder login (see apps/api/routers/auth.py) ---
    session_ttl_hours: int = 12
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    require_login_in_production: bool = True

    # --- Secrets management (see packages/core/secrets.py) ---
    # The ONE plaintext secret the system needs: the key that unlocks the encrypted
    # secrets store. Provide either the key itself or (preferred) a path to a file
    # containing it, readable only by the service user.
    secrets_master_key: Optional[str] = None
    secrets_master_key_file: Optional[str] = None

    # --- Acquisition engine (see docs/RUNBOOK.md) ---
    dns_validation: bool = True                      # MX/SPF/DMARC lookups; turn off only for offline development
    outbound_hard_ceiling: int = 30                  # absolute max sends/day regardless of autopilot config; raise deliberately
    report_email_to: Optional[str] = None            # where daily/weekly growth reports are emailed (via your own SMTP mailbox)
    approval_reminder_hours: float = 12.0            # remind about drafts waiting for approval longer than this
    hot_lead_sla_hours: float = 4.0                  # re-escalate an unanswered hot lead after this long

    # --- Mailbox TLS (see packages/core/tlsconfig.py) ---
    tls_ca_file: Optional[str] = None                # extra CA bundle to trust (private mail server / local test server)
    allow_insecure_tls: bool = False                 # DEV ONLY: disable certificate + hostname verification; refused in production

    # --- Worker operations (see scripts/run_worker.py, scripts/run_inbound.py) ---
    worker_poll_interval_seconds: float = 1.0
    worker_heartbeat_interval_seconds: float = 15.0
    worker_stale_after_seconds: float = 60.0
    inbound_poll_interval_seconds: float = 60.0
    maintenance_interval_seconds: float = 300.0

    def safe_dict(self) -> dict:
        """Settings with every secret/credential redacted -- the only form that
        may be logged, printed, or returned by an API."""
        from packages.core.log import redact
        return redact(self.model_dump())

    def __repr__(self) -> str:  # never leak secrets via a stray print(settings)
        return f"Settings({self.safe_dict()!r})"

    __str__ = __repr__

    @field_validator(
        "anthropic_api_key", "outreach_provider_api_key", "outreach_provider_webhook_secret",
        "smtp_host", "smtp_username", "smtp_password", "smtp_from_email",
        "imap_host", "imap_username", "imap_password", "slack_webhook_url", "founder_api_key",
        "secrets_master_key", "secrets_master_key_file", "report_email_to", "tls_ca_file", mode="before",
    )
    @classmethod
    def _blank_means_unset(cls, value):
        """`.env.example` ships lines like `FOUNDER_API_KEY=`. A blank value must mean 'not configured' (None),
        never an empty-string credential."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in ("production", "prod")

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        return [h.strip() for h in self.trusted_hosts.split(",") if h.strip()] or ["*"]

    @property
    def has_live_llm(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_live_outreach(self) -> bool:
        return bool(self.outreach_provider_api_key)

    @property
    def has_live_notifications(self) -> bool:
        return bool(self.slack_webhook_url)


@lru_cache
def get_settings() -> Settings:
    from packages.core.secrets import apply_secret_references
    return apply_secret_references(Settings())
