"""
Founder authentication logic, kept out of the route handlers so it is directly
unit-testable and so the routes stay thin (one responsibility per module).

Threat model handled here:
* Online guessing      -> per-account lockout persisted in the DB (survives restarts and
                          is shared by every API worker process) + per-IP rate limiting at the edge.
* Credential stuffing  -> generic "invalid credentials" for unknown email / wrong password /
                          inactive account; unknown emails still burn a real scrypt computation.
* DB / backup leak     -> passwords are scrypt hashes; sessions are stored only as SHA-256 of
                          the bearer token; TOTP seeds are Fernet-encrypted.
* Stolen session       -> server-side, revocable, fixed expiry; password change revokes all others.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from apps.api import security
from packages.core.config import get_settings
from packages.core.log import get_logger, log
from packages.core.secrets import decrypt_text, encrypt_text
from packages.db.models import FounderSession, FounderUser
from packages.db.session import session_scope

import logging

_log = get_logger("auth")
LAST_SEEN_THROTTLE_SECONDS = 60


class AuthError(Exception):
    status_code = 401
    detail = "Invalid credentials"

    def __init__(self, detail: Optional[str] = None, *, retry_after: Optional[int] = None):
        super().__init__(detail or self.detail)
        self.detail = detail or self.detail
        self.retry_after = retry_after


class InvalidCredentials(AuthError):
    status_code = 401


class TotpRequired(AuthError):
    status_code = 401
    detail = "totp_required"


class AccountLocked(AuthError):
    status_code = 429
    detail = "Too many failed attempts. Try again later."


@dataclass(frozen=True)
class Identity:
    user_id: Optional[uuid.UUID]
    email: str
    totp_enabled: bool = False
    via: str = "session"          # 'session' | 'api_key' | 'open'


@dataclass(frozen=True)
class LoginResult:
    token: str
    expires_at: datetime
    identity: Identity


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(email: str) -> str:
    return (email or "").strip().lower()


class AuthService:
    # ---- user management (CLI / tests) ------------------------------------
    @staticmethod
    def create_user(email: str, password: str) -> uuid.UUID:
        email = _norm(email)
        if "@" not in email or len(email) > 254:
            raise ValueError("A valid email address is required")
        security.validate_password_strength(password, email)
        with session_scope() as session:
            if session.query(FounderUser).filter_by(email=email).one_or_none() is not None:
                raise ValueError(f"A founder account for {email} already exists")
            user = FounderUser(id=uuid.uuid4(), email=email, password_hash=security.hash_password(password))
            session.add(user)
            session.flush()
            user_id = user.id
        log(_log, logging.INFO, "founder account created", email=email)
        return user_id

    @staticmethod
    def any_account() -> bool:
        """True if ANY founder account exists, active or not. Deliberately not 'any ACTIVE account':
        deactivating the only founder must lock the API, never silently drop it back into open dev mode."""
        with session_scope() as session:
            return session.query(FounderUser.id).first() is not None

    @staticmethod
    def set_password(email: str, new_password: str) -> None:
        """Operator reset (CLI). Revokes every session and clears any lockout."""
        email = _norm(email)
        security.validate_password_strength(new_password, email)
        with session_scope() as session:
            user = session.query(FounderUser).filter_by(email=email).one_or_none()
            if user is None:
                raise ValueError(f"No founder account for {email}")
            user.password_hash = security.hash_password(new_password)
            user.password_changed_at = _now()
            user.failed_attempts, user.locked_until = 0, None
            for s in session.query(FounderSession).filter_by(user_id=user.id, revoked_at=None).all():
                s.revoked_at = _now()

    @staticmethod
    def unlock(email: str) -> None:
        with session_scope() as session:
            user = session.query(FounderUser).filter_by(email=_norm(email)).one_or_none()
            if user is None:
                raise ValueError(f"No founder account for {email}")
            user.failed_attempts, user.locked_until = 0, None

    # ---- login / session ---------------------------------------------------
    @staticmethod
    def login(email: str, password: str, totp_code: Optional[str] = None, *, ip: Optional[str] = None,
              user_agent: Optional[str] = None) -> LoginResult:
        settings = get_settings()
        email = _norm(email)
        outcome: Optional[AuthError] = None
        result: Optional[LoginResult] = None

        with session_scope() as session:
            user = session.query(FounderUser).filter_by(email=email).with_for_update().one_or_none()

            if user is None or not user.is_active:
                security.verify_against_dummy(password)
                outcome = InvalidCredentials()
            else:
                now = _now()
                if user.locked_until is not None and user.locked_until <= now:
                    user.failed_attempts, user.locked_until = 0, None          # lock expired: fresh start
                if user.locked_until is not None:
                    outcome = AccountLocked(retry_after=max(1, int((user.locked_until - now).total_seconds())))
                elif not security.verify_password(password, user.password_hash):
                    outcome = AuthService._record_failure(user, settings, now)
                elif user.totp_secret_enc and not totp_code:
                    outcome = TotpRequired()                                     # password was right; not a failure
                elif user.totp_secret_enc and not security.verify_totp(decrypt_text(settings, user.totp_secret_enc), totp_code or ""):
                    outcome = AuthService._record_failure(user, settings, now)
                else:
                    token = security.new_session_token()
                    expires = now + timedelta(hours=settings.session_ttl_hours)
                    user.failed_attempts, user.locked_until, user.last_login_at = 0, None, now
                    session.add(FounderSession(
                        id=uuid.uuid4(), user_id=user.id, token_hash=security.hash_token(token),
                        expires_at=expires, ip=ip, user_agent=(user_agent or "")[:300] or None,
                    ))
                    result = LoginResult(token, expires, Identity(user.id, user.email, bool(user.totp_secret_enc)))

        if outcome is not None:
            log(_log, logging.WARNING, "login rejected", reason=type(outcome).__name__, email=email, ip=ip)
            raise outcome
        assert result is not None
        log(_log, logging.INFO, "login succeeded", email=email, ip=ip)
        return result

    @staticmethod
    def _record_failure(user: FounderUser, settings, now: datetime) -> AuthError:
        user.failed_attempts += 1
        if user.failed_attempts >= settings.login_max_failures:
            user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
            return AccountLocked(retry_after=settings.login_lockout_minutes * 60)
        return InvalidCredentials()

    @staticmethod
    def validate_token(token: str) -> Optional[Identity]:
        if not token:
            return None
        token_hash = security.hash_token(token)
        now = _now()
        with session_scope() as session:
            row = (
                session.query(FounderSession, FounderUser)
                .join(FounderUser, FounderUser.id == FounderSession.user_id)
                .filter(FounderSession.token_hash == token_hash)
                .one_or_none()
            )
            if row is None:
                return None
            sess, user = row
            if sess.revoked_at is not None or sess.expires_at <= now or not user.is_active:
                return None
            if sess.last_seen_at is None or (now - sess.last_seen_at).total_seconds() > LAST_SEEN_THROTTLE_SECONDS:
                sess.last_seen_at = now
            return Identity(user.id, user.email, bool(user.totp_secret_enc))

    @staticmethod
    def logout(token: str) -> bool:
        with session_scope() as session:
            sess = session.query(FounderSession).filter_by(token_hash=security.hash_token(token), revoked_at=None).one_or_none()
            if sess is None:
                return False
            sess.revoked_at = _now()
            return True

    @staticmethod
    def change_password(user_id: uuid.UUID, current_password: str, new_password: str, *, keep_token: Optional[str] = None) -> None:
        with session_scope() as session:
            user = session.get(FounderUser, user_id)
            if user is None or not security.verify_password(current_password, user.password_hash):
                raise InvalidCredentials("Current password is incorrect")
            security.validate_password_strength(new_password, user.email)
            user.password_hash = security.hash_password(new_password)
            user.password_changed_at = _now()
            keep_hash = security.hash_token(keep_token) if keep_token else None
            for s in session.query(FounderSession).filter_by(user_id=user_id, revoked_at=None).all():
                if s.token_hash != keep_hash:
                    s.revoked_at = _now()

    @staticmethod
    def purge_expired_sessions(older_than_days: int = 7) -> int:
        cutoff = _now() - timedelta(days=older_than_days)
        with session_scope() as session:
            return session.query(FounderSession).filter(FounderSession.expires_at < cutoff).delete()

    # ---- TOTP ----------------------------------------------------------------
    @staticmethod
    def begin_totp_setup(user_id: uuid.UUID) -> tuple[str, str]:
        with session_scope() as session:
            user = session.get(FounderUser, user_id)
            secret = security.generate_totp_secret()
            return secret, security.totp_provisioning_uri(secret, user.email)

    @staticmethod
    def enable_totp(user_id: uuid.UUID, secret: str, code: str) -> None:
        """Only turns 2FA on after the user proves their authenticator app holds the secret."""
        if not security.verify_totp(secret, code):
            raise InvalidCredentials("That code is not valid -- check your authenticator app's clock and try again")
        with session_scope() as session:
            session.get(FounderUser, user_id).totp_secret_enc = encrypt_text(get_settings(), secret)

    @staticmethod
    def disable_totp(user_id: uuid.UUID, password: str, code: str) -> None:
        settings = get_settings()
        with session_scope() as session:
            user = session.get(FounderUser, user_id)
            if not security.verify_password(password, user.password_hash):
                raise InvalidCredentials("Password is incorrect")
            if user.totp_secret_enc and not security.verify_totp(decrypt_text(settings, user.totp_secret_enc), code):
                raise InvalidCredentials("That code is not valid")
            user.totp_secret_enc = None
