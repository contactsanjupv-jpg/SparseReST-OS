"""Founder login: passwords, sessions, lockout, TOTP, API-key coexistence, and fail-closed production mode."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api import security
from apps.api.auth_service import AuthService
from apps.api.main import create_app
from packages.core.config import get_settings
from packages.core.secrets import generate_master_key
from packages.db.models import FounderSession, FounderUser
from packages.db.session import session_scope

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _clean_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def founder():
    email = f"founder-{uuid.uuid4().hex[:8]}@sparserest.test"
    user_id = AuthService.create_user(email, PASSWORD)
    yield {"email": email, "id": user_id, "password": PASSWORD}
    with session_scope() as session:
        session.query(FounderSession).filter_by(user_id=user_id).delete()
        session.query(FounderUser).filter_by(id=user_id).delete()


@pytest.fixture()
def client():
    return TestClient(create_app())     # a fresh app per test => fresh rate-limit buckets


def _login(client, founder, **extra):
    return client.post("/auth/login", json={"email": founder["email"], "password": founder["password"], **extra})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---- password primitives -------------------------------------------------------------

def test_password_hash_roundtrip_and_unique_salts():
    h1, h2 = security.hash_password(PASSWORD), security.hash_password(PASSWORD)
    assert h1 != h2 and PASSWORD not in h1
    assert security.verify_password(PASSWORD, h1) and not security.verify_password("wrong-password-123", h1)
    assert not security.verify_password(PASSWORD, "garbage") and not security.verify_password(PASSWORD, "scrypt$x$y$z$a$b")


@pytest.mark.parametrize("weak,email", [
    ("short1", ""), ("aaaaaaaaaaaaaaaa", ""), ("password1234", ""), ("myname-is-founder-1", "founder@x.com"),
])
def test_weak_passwords_rejected(weak, email):
    with pytest.raises(security.WeakPasswordError):
        security.validate_password_strength(weak, email)


def test_create_user_rejects_duplicates_and_bad_email(founder):
    with pytest.raises(ValueError, match="already exists"):
        AuthService.create_user(founder["email"].upper(), PASSWORD)
    with pytest.raises(ValueError):
        AuthService.create_user("not-an-email", PASSWORD)


# ---- login / sessions ------------------------------------------------------------------

def test_login_grants_access_and_requires_it(client, founder):
    assert client.get("/tasks").status_code == 401                          # an account exists => login required
    r = _login(client, founder)
    assert r.status_code == 200
    token = r.json()["token"]
    assert client.get("/tasks", headers=_auth(token)).status_code == 200
    assert client.get("/auth/me", headers=_auth(token)).json()["email"] == founder["email"]


def test_token_is_stored_only_as_a_hash(client, founder):
    token = _login(client, founder).json()["token"]
    with session_scope() as session:
        stored = [r.token_hash for r in session.query(FounderSession).filter_by(user_id=founder["id"]).all()]
    assert token not in stored and security.hash_token(token) in stored


def test_wrong_password_and_unknown_email_are_indistinguishable(client, founder):
    wrong = client.post("/auth/login", json={"email": founder["email"], "password": "definitely-wrong-pw"})
    unknown = client.post("/auth/login", json={"email": "nobody@sparserest.test", "password": "definitely-wrong-pw"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_logout_revokes_the_session(client, founder):
    token = _login(client, founder).json()["token"]
    assert client.post("/auth/logout", headers=_auth(token)).status_code == 204
    assert client.get("/tasks", headers=_auth(token)).status_code == 401


def test_expired_session_is_rejected(client, founder):
    token = _login(client, founder).json()["token"]
    with session_scope() as session:
        session.query(FounderSession).filter_by(user_id=founder["id"]).update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    assert client.get("/tasks", headers=_auth(token)).status_code == 401


def test_deactivated_user_loses_access_immediately(client, founder):
    token = _login(client, founder).json()["token"]
    with session_scope() as session:
        session.get(FounderUser, founder["id"]).is_active = False
    assert client.get("/tasks", headers=_auth(token)).status_code == 401
    assert _login(client, founder).status_code == 401


def test_garbage_and_malformed_bearer_headers_rejected(client, founder):
    for header in ({"Authorization": "Bearer "}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer not-a-real-token"}, {}):
        assert client.get("/tasks", headers=header).status_code == 401


def test_oversized_credentials_rejected_before_hashing(client):
    r = client.post("/auth/login", json={"email": "a@b.co", "password": "x" * 100_000})
    assert r.status_code in (413, 422)


# ---- lockout ---------------------------------------------------------------------------

def test_account_locks_after_repeated_failures_and_persists(client, founder):
    limit = get_settings().login_max_failures
    for _ in range(limit - 1):
        assert client.post("/auth/login", json={"email": founder["email"], "password": "wrong-password-xx"}).status_code == 401
    locked = client.post("/auth/login", json={"email": founder["email"], "password": "wrong-password-xx"})
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0

    # Even the CORRECT password is refused while locked -- and the lock lives in the DB, not in process memory.
    assert _login(client, founder).status_code == 429
    with session_scope() as session:
        assert session.get(FounderUser, founder["id"]).locked_until is not None


def test_lock_expires_and_counter_resets(client, founder):
    for _ in range(get_settings().login_max_failures):
        client.post("/auth/login", json={"email": founder["email"], "password": "wrong-password-xx"})
    with session_scope() as session:
        session.get(FounderUser, founder["id"]).locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert _login(client, founder).status_code == 200
    with session_scope() as session:
        user = session.get(FounderUser, founder["id"])
        assert user.failed_attempts == 0 and user.locked_until is None


def test_successful_login_resets_failure_counter(client, founder):
    client.post("/auth/login", json={"email": founder["email"], "password": "wrong-password-xx"})
    assert _login(client, founder).status_code == 200
    with session_scope() as session:
        assert session.get(FounderUser, founder["id"]).failed_attempts == 0


def test_operator_unlock(client, founder):
    for _ in range(get_settings().login_max_failures):
        client.post("/auth/login", json={"email": founder["email"], "password": "wrong-password-xx"})
    AuthService.unlock(founder["email"])
    assert _login(client, founder).status_code == 200


# ---- change password ---------------------------------------------------------------------

def test_change_password_revokes_other_sessions_keeps_current(client, founder):
    current = _login(client, founder).json()["token"]
    other = _login(client, founder).json()["token"]
    new_pw = "a-brand-new-long-passphrase"
    r = client.post("/auth/change-password", headers=_auth(current), json={"current_password": PASSWORD, "new_password": new_pw})
    assert r.status_code == 204
    assert client.get("/tasks", headers=_auth(current)).status_code == 200
    assert client.get("/tasks", headers=_auth(other)).status_code == 401
    assert client.post("/auth/login", json={"email": founder["email"], "password": PASSWORD}).status_code == 401
    assert client.post("/auth/login", json={"email": founder["email"], "password": new_pw}).status_code == 200


def test_change_password_validation(client, founder):
    token = _login(client, founder).json()["token"]
    wrong_current = client.post("/auth/change-password", headers=_auth(token), json={"current_password": "nope-nope-nope-1", "new_password": "another-long-passphrase-1"})
    weak_new = client.post("/auth/change-password", headers=_auth(token), json={"current_password": PASSWORD, "new_password": "short"})
    assert wrong_current.status_code == 401 and weak_new.status_code == 422


# ---- TOTP -----------------------------------------------------------------------------------

def test_totp_rfc6238_reference_vector():
    # RFC 6238 Appendix B test vector (SHA-1, secret "12345678901234567890"), 6 digits of the 8-digit values.
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert security.totp_code(secret, at=59) == "287082"
    assert security.totp_code(secret, at=1111111109) == "081804"


def test_totp_enrolment_login_flow(client, founder, monkeypatch):
    monkeypatch.setenv("SECRETS_MASTER_KEY", generate_master_key())
    get_settings.cache_clear()
    token = _login(client, founder).json()["token"]

    setup = client.post("/auth/totp/setup", headers=_auth(token)).json()
    secret = setup["secret"]
    assert setup["otpauth_uri"].startswith("otpauth://totp/")

    bad = client.post("/auth/totp/enable", headers=_auth(token), json={"secret": secret, "code": "000000"})
    assert bad.status_code == 401
    assert client.post("/auth/totp/enable", headers=_auth(token), json={"secret": secret, "code": security.totp_code(secret)}).status_code == 204

    # Now a password alone is not enough...
    needs = _login(client, founder)
    assert needs.status_code == 401 and needs.json()["detail"] == "totp_required"
    # ...a wrong code is a failure (counts toward lockout), a right one succeeds.
    assert _login(client, founder, totp_code="123456").status_code == 401
    ok = _login(client, founder, totp_code=security.totp_code(secret))
    assert ok.status_code == 200 and ok.json()["totp_enabled"] is True

    # The seed is encrypted at rest.
    with session_scope() as session:
        stored = session.get(FounderUser, founder["id"]).totp_secret_enc
    assert stored and secret not in stored

    off = client.post("/auth/totp/disable", headers=_auth(ok.json()["token"]), json={"password": PASSWORD, "code": security.totp_code(secret)})
    assert off.status_code == 204
    assert _login(client, founder).status_code == 200


def test_totp_failures_count_toward_lockout(client, founder, monkeypatch):
    monkeypatch.setenv("SECRETS_MASTER_KEY", generate_master_key())
    get_settings.cache_clear()
    secret = security.generate_totp_secret()
    AuthService.enable_totp(founder["id"], secret, security.totp_code(secret))
    for _ in range(get_settings().login_max_failures):
        _login(client, founder, totp_code="111111")
    assert _login(client, founder, totp_code=security.totp_code(secret)).status_code == 429


# ---- API key coexistence ---------------------------------------------------------------------

def test_api_key_still_works_and_is_checked(client, monkeypatch):
    monkeypatch.setenv("FOUNDER_API_KEY", "service-key-abc123")
    get_settings.cache_clear()
    assert client.get("/tasks").status_code == 401
    assert client.get("/tasks", headers=_auth("wrong")).status_code == 401
    assert client.get("/tasks", headers=_auth("service-key-abc123")).status_code == 200
    # A service key is not a person: it can't manage account credentials.
    assert client.post("/auth/change-password", headers=_auth("service-key-abc123"),
                       json={"current_password": "x" * 12, "new_password": "y" * 12}).status_code == 403


def test_auth_status_reports_whether_login_is_needed(client, founder):
    body = client.get("/auth/status").json()
    assert body["login_required"] is True and body["accounts_exist"] is True


# ---- production posture -----------------------------------------------------------------------------

def test_production_fails_closed_when_no_auth_configured(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    with session_scope() as session:
        assert session.query(FounderUser).count() == 0      # precondition: no accounts in the test DB
    prod = TestClient(create_app())
    assert prod.get("/tasks").status_code == 503
    assert prod.get("/health/live").status_code == 200
    assert prod.get("/docs").status_code == 404 and prod.get("/openapi.json").status_code == 404


def test_health_does_not_leak_driver_errors_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody:secretpw@127.0.0.1:1/none")
    get_settings.cache_clear()
    import packages.db.session as dbs
    saved = dbs._engine
    dbs._engine = None
    try:
        r = TestClient(create_app()).get("/health")
        assert r.status_code == 503 and "detail" not in r.json() and "secretpw" not in r.text
    finally:
        dbs._engine = saved
