"""Pre-flight: refuses (exit 1) if the deployment is unsafe to expose. Run before every production start.
    ENVIRONMENT=production python -m scripts.check_production"""
from __future__ import annotations

import sys

from packages.core.config import get_settings


def audit(settings, *, accounts_exist: bool, migrations_ok: bool) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if not settings.is_production:
        warnings.append("ENVIRONMENT is not 'production' -- strict checks skipped")
    else:
        if not (accounts_exist or settings.founder_api_key):
            errors.append("No founder account and no FOUNDER_API_KEY: create one with scripts/create_founder.py")
        if "*" in settings.cors_origins or any(o.startswith("http://") and "localhost" not in o for o in settings.cors_origins):
            errors.append("ALLOWED_ORIGINS must be explicit https origins")
        if settings.cors_origins == ["http://localhost:3000"]:
            errors.append("ALLOWED_ORIGINS is still the localhost default; set your real frontend origin")
        if settings.trusted_host_list == ["*"]:
            errors.append("TRUSTED_HOSTS is '*'; set your API hostname(s)")
        if "change_me_in_env" in settings.app_database_url:
            errors.append("APP_DATABASE_URL still uses the placeholder app_role password (change_me_in_env)")
        if not (settings.secrets_master_key or settings.secrets_master_key_file):
            warnings.append("No secrets master key configured: secret:// references are unavailable")
        if settings.secrets_master_key and not settings.secrets_master_key_file:
            warnings.append("Master key is in an env var; prefer SECRETS_MASTER_KEY_FILE (0600 file)")
        if settings.allow_insecure_tls:
            errors.append("ALLOW_INSECURE_TLS is on: mailbox certificates would not be verified")
        if not settings.rate_limit_enabled:
            errors.append("RATE_LIMIT_ENABLED is off")
        if settings.founder_api_key and len(settings.founder_api_key) < 32:
            errors.append("FOUNDER_API_KEY is shorter than 32 characters")
    if not migrations_ok:
        errors.append("Database migrations are not up to date (alembic upgrade head)")
    if settings.outreach_mode == "smtp" and not settings.has_live_notifications:
        warnings.append("Live outreach with no SLACK_WEBHOOK_URL: hot-lead and failure alerts only reach the console")
    return errors, warnings


def main() -> int:
    from apps.api.auth_service import AuthService
    from apps.api.routers.health import schema_state
    settings = get_settings()
    try:
        migrations_ok = schema_state()["up_to_date"]
    except Exception:
        migrations_ok = False
    errors, warnings = audit(settings, accounts_exist=AuthService.any_account() if migrations_ok else False, migrations_ok=migrations_ok)
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"FAIL  {e}")
    print("OK" if not errors else f"{len(errors)} blocking problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
