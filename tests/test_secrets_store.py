"""Encrypted secrets store + secret:// / file:// configuration references."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from packages.core.config import Settings
from packages.core.secrets import (
    MissingMasterKey, SecretsError, SecretStore, _engine_for, apply_secret_references,
    generate_master_key, load_master_keys, resolve_reference,
)
from packages.db.session import get_engine

PREFIX = "t-secret-"


def _name(label: str) -> str:
    return f"t{uuid.uuid4().hex[:8]}.{label}"


@pytest.fixture()
def key() -> str:
    return generate_master_key()


@pytest.fixture()
def store(key):
    s = SecretStore([key], get_engine())
    yield s
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM secrets WHERE name ~ '^t[0-9a-f]{8}\\.'"))


def test_roundtrip_and_versioning(store):
    name = _name("smtp")
    assert store.set(name, "hunter2") == 1
    assert store.get(name) == "hunter2"
    assert store.set(name, "hunter3") == 2
    assert store.get(name) == "hunter3"
    assert name in [s["name"] for s in store.list_names()]


def test_database_holds_only_ciphertext(store):
    name = _name("db")
    store.set(name, "plaintext-credential-XYZ")
    with get_engine().connect() as conn:
        raw = conn.execute(text("SELECT ciphertext FROM secrets WHERE name=:n"), {"n": name}).scalar_one()
    assert "plaintext-credential-XYZ" not in raw
    assert name not in raw


def test_wrong_master_key_cannot_read(store):
    name = _name("wrongkey")
    store.set(name, "topsecret")
    other = SecretStore([generate_master_key()], get_engine())
    with pytest.raises(SecretsError) as exc:
        other.get(name)
    assert "topsecret" not in str(exc.value)


def test_ciphertext_swap_between_names_is_detected(store):
    a, b = _name("a"), _name("b")
    store.set(a, "value-a")
    store.set(b, "value-b")
    with get_engine().begin() as conn:
        ct_a = conn.execute(text("SELECT ciphertext FROM secrets WHERE name=:n"), {"n": a}).scalar_one()
        conn.execute(text("UPDATE secrets SET ciphertext=:c WHERE name=:n"), {"c": ct_a, "n": b})
    with pytest.raises(SecretsError, match="integrity"):
        store.get(b)


def test_tampered_ciphertext_is_rejected(store):
    name = _name("tamper")
    store.set(name, "v")
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE secrets SET ciphertext = substr(ciphertext,1,20) || 'AAAA' || substr(ciphertext,25) WHERE name=:n"), {"n": name})
    with pytest.raises(SecretsError):
        store.get(name)


def test_missing_secret_raises_clearly(store):
    with pytest.raises(SecretsError, match="does not exist"):
        store.get(_name("nope"))


@pytest.mark.parametrize("bad", ["", "Has Space", "UPPER", "../etc", "a" * 80, "9start"])
def test_bad_names_rejected(store, bad):
    with pytest.raises(SecretsError):
        store.set(bad, "x")


def test_empty_value_rejected(store):
    with pytest.raises(SecretsError):
        store.set(_name("empty"), "")


def test_delete(store):
    name = _name("del")
    store.set(name, "x")
    assert store.delete(name) is True
    assert store.delete(name) is False


def test_master_key_rotation(store, key):
    name = _name("rot")
    store.set(name, "rotate-me")
    new_key = generate_master_key()

    # Step 1: prepend the new key -- old ciphertext still readable, new writes use new key.
    both = SecretStore([new_key, key], get_engine())
    assert both.get(name) == "rotate-me"
    # Step 2: re-encrypt everything under the primary key.
    assert both.rotate_all() >= 1
    # Step 3: the old key alone no longer works; the new key alone does.
    assert SecretStore([new_key], get_engine()).get(name) == "rotate-me"
    with pytest.raises(SecretsError):
        SecretStore([key], get_engine()).get(name)


def test_invalid_master_key_format_is_a_clear_error():
    with pytest.raises(MissingMasterKey, match="not a valid Fernet key"):
        SecretStore(["not-a-key"], get_engine())


def test_load_master_keys_from_file_and_permissions(tmp_path, key):
    f = tmp_path / "master.key"
    f.write_text(key + "\n")
    os.chmod(f, 0o600)
    s = Settings(secrets_master_key_file=str(f), environment="production")
    assert load_master_keys(s) == [key]

    os.chmod(f, 0o644)
    with pytest.raises(MissingMasterKey, match="readable by group/others"):
        load_master_keys(s)
    # Non-production is lenient (local dev convenience); production is strict.
    assert load_master_keys(Settings(secrets_master_key_file=str(f), environment="development")) == [key]


def test_load_master_keys_requires_one():
    with pytest.raises(MissingMasterKey):
        load_master_keys(Settings(secrets_master_key=None, secrets_master_key_file=None))


def test_resolve_reference_passthrough_file_and_store(store, tmp_path):
    name = _name("ref")
    store.set(name, "from-store")
    f = tmp_path / "cred"
    f.write_text("from-file\n")
    assert resolve_reference("literal", store_factory=lambda: store) == "literal"
    assert resolve_reference(None, store_factory=lambda: store) is None
    assert resolve_reference(f"secret://{name}", store_factory=lambda: store) == "from-store"
    assert resolve_reference(f"file://{f}", store_factory=lambda: store) == "from-file"
    with pytest.raises(SecretsError):
        resolve_reference("file:///does/not/exist", store_factory=lambda: store)


def test_bootstrap_urls_cannot_use_store_references():
    with pytest.raises(SecretsError, match="not allowed here"):
        resolve_reference("secret://db_url", store_factory=lambda: None, allow_store=False)


def test_apply_secret_references_end_to_end(store, key):
    name = _name("smtp_password")
    store.set(name, "real-smtp-password")
    settings = Settings(smtp_password=f"secret://{name}", secrets_master_key=key, imap_password="plain-imap")
    apply_secret_references(settings)
    assert settings.smtp_password == "real-smtp-password"
    assert settings.imap_password == "plain-imap"


def test_unresolvable_reference_fails_closed_and_names_field_not_value(key):
    settings = Settings(smtp_password="secret://t00000000.missing", secrets_master_key=key)
    with pytest.raises(SecretsError) as exc:
        apply_secret_references(settings)
    assert "SMTP_PASSWORD" in str(exc.value)


def test_database_url_may_be_a_file_reference(tmp_path):
    f = tmp_path / "dburl"
    f.write_text("postgresql+psycopg://u:p@h/db\n")
    settings = Settings(database_url=f"file://{f}")
    apply_secret_references(settings)
    assert settings.database_url == "postgresql+psycopg://u:p@h/db"


def test_settings_repr_never_leaks_secrets():
    settings = Settings(smtp_password="hunter2", founder_api_key="founder-key-xyz", anthropic_api_key="sk-ant-123",
                        app_database_url="postgresql+psycopg://app_role:dbpass@h/db")
    for rendered in (repr(settings), str(settings), str(settings.safe_dict())):
        for leaked in ("hunter2", "founder-key-xyz", "sk-ant-123", "dbpass"):
            assert leaked not in rendered
