"""
Secrets management at $0: an encrypted secrets store in the Postgres this system
already requires, plus `secret://` and `file://` references in configuration.

How it works
------------
* Secrets live in the `secrets` table as Fernet ciphertext (AES-128-CBC +
  HMAC-SHA256, from the `cryptography` package). The database alone is useless:
  reading the table gives ciphertext.
* The ONE plaintext secret left is the master key that unlocks the store. It is
  supplied as `SECRETS_MASTER_KEY_FILE` (preferred: a file readable only by the
  service user, which is also exactly how Docker/systemd credentials are
  delivered) or `SECRETS_MASTER_KEY`. A comma-separated list of keys is accepted:
  the FIRST encrypts, all of them decrypt -- that is the rotation mechanism
  (prepend a new key, run `scripts/secrets_cli.py rotate`, drop the old key).
* Configuration values may be references instead of literals:
      SMTP_PASSWORD=secret://smtp_password     -> decrypted from the store
      ANTHROPIC_API_KEY=file:///run/secrets/anthropic_key   -> read from a file
  so `.env` can contain no real credentials at all.
* Each ciphertext embeds its own name, so swapping one row's ciphertext into
  another row is detected rather than silently accepted.
* The `secrets` table is explicitly revoked from `app_role` (see the migration):
  agent/tool code, which runs as app_role, cannot read it even through a bug.

What this is not: an HSM or a hosted vault with audit/expiry. It is honest,
audited-by-tests envelope encryption that removes plaintext credentials from
`.env` and the database, at zero cost. Tracked in docs/SECURITY.md.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_NAME_RE = re.compile(r"^[a-z][a-z0-9_.\-]{0,62}$")

# Settings fields that may hold a `secret://` / `file://` reference.
SECRET_FIELDS = (
    "anthropic_api_key", "outreach_provider_api_key", "outreach_provider_webhook_secret",
    "smtp_password", "imap_password", "slack_webhook_url", "founder_api_key",
)
# Bootstrap fields: needed to reach the store at all, so only `file://` is allowed.
FILE_ONLY_FIELDS = ("database_url", "app_database_url")


class SecretsError(Exception):
    """Base class. Messages never contain secret values."""


class MissingMasterKey(SecretsError):
    pass


def generate_master_key() -> str:
    return Fernet.generate_key().decode()


def _read_key_file(path: str, *, enforce_permissions: bool) -> str:
    p = Path(path)
    if not p.is_file():
        raise MissingMasterKey(f"SECRETS_MASTER_KEY_FILE points at '{path}', which does not exist")
    if enforce_permissions and os.name == "posix":
        mode = p.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise MissingMasterKey(
                f"Master key file '{path}' is readable by group/others (mode {oct(mode & 0o777)}). "
                "Run: chmod 600 on it."
            )
    return p.read_text().strip()


def load_master_keys(settings) -> list[str]:
    raw = None
    if settings.secrets_master_key_file:
        raw = _read_key_file(settings.secrets_master_key_file, enforce_permissions=settings.is_production)
    elif settings.secrets_master_key:
        raw = settings.secrets_master_key
    if not raw:
        raise MissingMasterKey("No master key configured: set SECRETS_MASTER_KEY_FILE (preferred) or SECRETS_MASTER_KEY")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise MissingMasterKey("Master key is empty")
    return keys


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name or ""):
        raise SecretsError("Secret names must match ^[a-z][a-z0-9_.-]{0,62}$")


class SecretStore:
    def __init__(self, master_keys: list[str], engine: Engine):
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in master_keys])
        except (ValueError, TypeError) as exc:
            raise MissingMasterKey("Master key is not a valid Fernet key (generate one with scripts/secrets_cli.py gen-key)") from exc
        self._engine = engine

    @classmethod
    def from_settings(cls, settings) -> "SecretStore":
        return cls(load_master_keys(settings), _engine_for(settings.database_url))

    # -- write / read -----------------------------------------------------
    def set(self, name: str, value: str) -> int:
        _validate_name(name)
        if not isinstance(value, str) or value == "":
            raise SecretsError("Secret value must be a non-empty string")
        token = self._fernet.encrypt(json.dumps({"n": name, "v": value}).encode()).decode()
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                INSERT INTO secrets (name, ciphertext, version) VALUES (:n, :c, 1)
                ON CONFLICT (name) DO UPDATE
                   SET ciphertext = EXCLUDED.ciphertext, version = secrets.version + 1, updated_at = now()
                RETURNING version
            """), {"n": name, "c": token}).one()
        return int(row[0])

    def get(self, name: str) -> str:
        _validate_name(name)
        with self._engine.connect() as conn:
            row = conn.execute(text("SELECT ciphertext FROM secrets WHERE name = :n"), {"n": name}).one_or_none()
        if row is None:
            raise SecretsError(f"Secret '{name}' does not exist in the store")
        return self._decrypt(name, row[0])

    def _decrypt(self, name: str, token: str) -> str:
        try:
            payload = json.loads(self._fernet.decrypt(token.encode()))
        except InvalidToken as exc:
            raise SecretsError(f"Secret '{name}' could not be decrypted (wrong master key, or the ciphertext was tampered with)") from exc
        if payload.get("n") != name:
            raise SecretsError(f"Secret '{name}' failed integrity check: ciphertext belongs to a different name")
        return payload["v"]

    def delete(self, name: str) -> bool:
        _validate_name(name)
        with self._engine.begin() as conn:
            return conn.execute(text("DELETE FROM secrets WHERE name = :n"), {"n": name}).rowcount > 0

    def list_names(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(text("SELECT name, version, updated_at FROM secrets ORDER BY name")).all()
        return [{"name": r[0], "version": r[1], "updated_at": r[2].isoformat()} for r in rows]

    def rotate_all(self) -> int:
        """Re-encrypt every secret under the PRIMARY (first) master key. Run after
        prepending a new key; afterwards the old key can be dropped."""
        count = 0
        with self._engine.begin() as conn:
            rows = conn.execute(text("SELECT name, ciphertext FROM secrets FOR UPDATE")).all()
            for name, token in rows:
                self._decrypt(name, token)  # refuse to "rotate" something already corrupt
                new_token = self._fernet.rotate(token.encode()).decode()
                conn.execute(text("UPDATE secrets SET ciphertext = :c, version = version + 1, updated_at = now() WHERE name = :n"),
                             {"c": new_token, "n": name})
                count += 1
        return count


_engines: dict[str, Engine] = {}


def _engine_for(database_url: str) -> Engine:
    """A private admin engine for the store. Deliberately NOT packages.db.session's
    get_engine(): that calls get_settings(), and get_settings() calls us."""
    if database_url not in _engines:
        _engines[database_url] = create_engine(database_url, future=True, pool_pre_ping=True)
    return _engines[database_url]


def resolve_reference(value: Optional[str], *, store_factory: Callable[[], SecretStore], allow_store: bool = True) -> Optional[str]:
    if not value or not isinstance(value, str):
        return value
    if value.startswith("secret://"):
        if not allow_store:
            raise SecretsError("secret:// references are not allowed here (this value is needed to reach the store itself); use file://")
        return store_factory().get(value[len("secret://"):])
    if value.startswith("file://"):
        path = Path(value[len("file://"):])
        if not path.is_file():
            raise SecretsError(f"file reference '{path}' does not exist")
        return path.read_text().strip()
    return value


def apply_secret_references(settings):
    """Replace every `secret://` / `file://` reference in a Settings object with
    its real value. Fails closed: an unresolvable reference aborts startup with
    a message naming the FIELD (never the value)."""
    store: dict[str, SecretStore] = {}

    def factory() -> SecretStore:
        if "s" not in store:
            store["s"] = SecretStore.from_settings(settings)
        return store["s"]

    for field in FILE_ONLY_FIELDS:
        current = getattr(settings, field, None)
        if isinstance(current, str) and current.startswith(("secret://", "file://")):
            try:
                setattr(settings, field, resolve_reference(current, store_factory=factory, allow_store=False))
            except SecretsError as exc:
                raise SecretsError(f"Cannot resolve {field.upper()}: {exc}") from exc

    for field in SECRET_FIELDS:
        current = getattr(settings, field, None)
        if isinstance(current, str) and current.startswith(("secret://", "file://")):
            try:
                setattr(settings, field, resolve_reference(current, store_factory=factory))
            except SecretsError as exc:
                raise SecretsError(f"Cannot resolve {field.upper()}: {exc}") from exc
    return settings


# ---------------------------------------------------------------------------
# Small encrypt/decrypt helpers for values that must be RECOVERABLE but don't
# belong in the named store (e.g. a founder's TOTP seed, which the server has
# to read back to verify codes). Same master key, same Fernet envelope.
# ---------------------------------------------------------------------------

def _multi_fernet(settings) -> MultiFernet:
    try:
        return MultiFernet([Fernet(k.encode()) for k in load_master_keys(settings)])
    except (ValueError, TypeError) as exc:
        raise MissingMasterKey("Master key is not a valid Fernet key") from exc


def encrypt_text(settings, plaintext: str) -> str:
    return _multi_fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt_text(settings, token: str) -> str:
    try:
        return _multi_fernet(settings).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretsError("Stored value could not be decrypted (wrong master key, or tampered)") from exc
