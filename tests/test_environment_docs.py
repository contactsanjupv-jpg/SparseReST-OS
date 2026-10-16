"""config.py is the source of truth: every setting must be documented in docs/ENVIRONMENT.md and listed in .env.example,
and nothing documented may be a setting the code doesn't read. Also: no committed file contains a real-looking secret."""
from __future__ import annotations

import re
from pathlib import Path

from packages.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {name.upper() for name in Settings.model_fields}


def _documented_vars() -> set[str]:
    text = (ROOT / "docs" / "ENVIRONMENT.md").read_text()
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)`", text, re.M))


def test_every_setting_is_documented():
    assert not FIELDS - _documented_vars(), f"undocumented settings: {sorted(FIELDS - _documented_vars())}"


def test_nothing_documented_that_the_code_does_not_read():
    assert not _documented_vars() - FIELDS, f"stale docs: {sorted(_documented_vars() - FIELDS)}"


def test_env_example_lists_every_setting():
    example = (ROOT / ".env.example").read_text()
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", example, re.M))
    assert not FIELDS - listed, f"missing from .env.example: {sorted(FIELDS - listed)}"


def test_env_example_has_no_real_secrets():
    for line in (ROOT / ".env.example").read_text().splitlines():
        key, _, value = line.partition("=")
        if re.search(r"(PASSWORD|KEY|SECRET|TOKEN)", key) and value.strip():
            assert value.startswith(("secret://", "file://")) or value in ("change_me_in_env",) or "PASSWORD" not in key and value == "", line


def test_no_secret_shaped_strings_in_source():
    pattern = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|xox[bp]-[A-Za-z0-9\-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
    skip = {".git", "node_modules", ".next", "__pycache__", ".venv", "venv", "env", "site-packages", ".pytest_cache"}
    for path in ROOT.rglob("*"):
        if path.is_file() and not (set(path.parts) & skip) and path.suffix in (".py", ".js", ".md", ".yml", ".yaml", ".env", ".example", ".service", ".json"):
            assert not pattern.search(path.read_text(errors="ignore")), f"secret-shaped string in {path}"


def test_compose_and_units_reference_real_modules():
    for text in ((ROOT / "docker-compose.yml").read_text(), *(p.read_text() for p in (ROOT / "infra" / "systemd").glob("*.service"))):
        for mod in re.findall(r"scripts\.(\w+)", text):
            assert (ROOT / "scripts" / f"{mod}.py").exists(), mod


def test_blank_env_values_mean_unset(monkeypatch):
    """Copying .env.example verbatim (blank credential lines) must behave like leaving them out."""
    for name in ("FOUNDER_API_KEY", "SMTP_HOST", "SLACK_WEBHOOK_URL", "ANTHROPIC_API_KEY", "SECRETS_MASTER_KEY"):
        monkeypatch.setenv(name, "")
    s = Settings()
    assert s.founder_api_key is None and s.smtp_host is None and s.slack_webhook_url is None
    assert s.has_live_llm is False and s.has_live_notifications is False
