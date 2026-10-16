"""
Test isolation: the suite NEVER touches the real company database.

Before anything imports application code, this points DATABASE_URL / APP_DATABASE_URL at a
dedicated `<name>_test` database, drops and recreates it for every pytest session, applies all
migrations from scratch, and seeds it. Consequences:
  * tests can never leave junk (fake prospects, approvals, tasks) in the database you run the company on;
  * every run also proves the migrations apply cleanly on an empty database;
  * a hard guard refuses to run if the resolved database name doesn't end in `_test`.

Requires the DB user to be allowed to CREATE/DROP DATABASE (true for the default `postgres` superuser).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The suite must behave identically with or without a populated developer .env. Applied BEFORE any application
# module is imported: Settings reads its env file at class-definition time.
os.environ["SPARSEREST_ENV_FILE"] = ""

import psycopg
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _env_file_value(name: str) -> str | None:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip()
    return None


def _resolve(name: str, default: str) -> str:
    return os.environ.get(name) or _env_file_value(name) or default


def _test_url(url: str) -> str:
    parsed = make_url(url)
    base = parsed.database or "sparserest_dev"
    return parsed.set(database=base if base.endswith("_test") else f"{base}_test").render_as_string(hide_password=False)


_DB_URL = _resolve("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/sparserest_dev")
_APP_URL = _resolve("APP_DATABASE_URL", "postgresql+psycopg://app_role:change_me_in_env@localhost:5432/sparserest_dev")
TEST_DATABASE_URL = _test_url(_DB_URL)
def _scrub_settings_from_environment() -> None:
    """Remove every environment variable the application reads, so a developer's shell/.env values cannot change
    test behaviour. Tests that need a setting set it explicitly (monkeypatch.setenv + get_settings.cache_clear())."""
    from packages.core.config import Settings
    for field in Settings.model_fields:
        os.environ.pop(field.upper(), None)


_scrub_settings_from_environment()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["APP_DATABASE_URL"] = _test_url(_APP_URL)
# Tests must be deterministic and free regardless of what the developer's .env enables.
os.environ["DNS_VALIDATION"] = "false"
for _name in ("ENVIRONMENT", "ENRICHMENT_MODE", "OUTREACH_MODE", "ANTHROPIC_API_KEY", "SLACK_WEBHOOK_URL",
              "FOUNDER_API_KEY", "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL",
              "IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD", "REPORT_EMAIL_TO"):
    os.environ[_name] = {"ENVIRONMENT": "development", "ENRICHMENT_MODE": "stub", "OUTREACH_MODE": "stub"}.get(_name, "")


def _admin_dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)


def pytest_configure(config):
    url = make_url(TEST_DATABASE_URL)
    if not (url.database or "").endswith("_test"):                       # the guard: never run against anything else
        raise RuntimeError(f"Refusing to run tests: database '{url.database}' is not a *_test database")
    name = url.database
    with psycopg.connect(_admin_dsn(TEST_DATABASE_URL), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')

    from alembic import command
    from alembic.config import Config
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "packages" / "db" / "migrations"))
    os.chdir(ROOT)
    command.upgrade(cfg, "head")

    from packages.core.config import get_settings
    get_settings.cache_clear()
    from scripts import seed
    seed.run()
