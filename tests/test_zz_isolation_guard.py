"""Proves the suite is running against the isolated test database, not the company database."""
from packages.core.config import get_settings
from sqlalchemy.engine import make_url


def test_suite_runs_against_a_dedicated_test_database():
    assert make_url(get_settings().database_url).database.endswith("_test")
    assert make_url(get_settings().app_database_url).database.endswith("_test")


def test_real_integrations_are_disabled_under_test():
    s = get_settings()
    assert s.outreach_mode == "stub" and s.enrichment_mode == "stub" and not s.has_live_llm and not s.has_live_notifications


def test_developer_env_file_is_never_read_under_test():
    """Regression: a populated .env (master key file, rate limit off, SMTP creds...) used to change test behaviour."""
    import os
    from packages.core.config import Settings
    assert Settings.model_config["env_file"] is None
    assert os.environ["SPARSEREST_ENV_FILE"] == ""


def test_application_settings_do_not_leak_in_from_the_environment():
    import os
    from packages.core.config import Settings, get_settings
    allowed = {"DATABASE_URL", "APP_DATABASE_URL", "ENVIRONMENT", "ENRICHMENT_MODE", "OUTREACH_MODE", "DNS_VALIDATION"}
    leaked = {f.upper(): os.environ[f.upper()] for f in Settings.model_fields if f.upper() in os.environ and f.upper() not in allowed and os.environ[f.upper()]}
    assert leaked == {}
    s = get_settings()
    assert s.secrets_master_key is None and s.secrets_master_key_file is None and s.rate_limit_enabled is True and s.founder_api_key is None
