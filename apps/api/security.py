"""
Credential primitives for founder login -- password hashing, session tokens,
TOTP. Standard library only (hashlib.scrypt, hmac, secrets): no paid or
third-party auth provider, and nothing here needs network access.

Decisions worth knowing:
* scrypt (memory-hard) with a per-password random salt; parameters are stored in
  the hash string so they can be raised later without invalidating old hashes.
* Session tokens are 256-bit random values. Only their SHA-256 is stored, so a
  database leak does not yield usable sessions.
* TOTP is RFC 6238 (SHA-1, 6 digits, 30s) -- compatible with Google Authenticator,
  1Password, Authy, etc. -- with a +/-1 step tolerance for clock drift.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD_LENGTH = 12
_COMMON_PASSWORDS = {
    "password1234", "123456789012", "qwertyuiop12", "letmein12345", "administrator",
    "changeme1234", "welcome12345", "iloveyou1234", "passwordpassword",
}


class WeakPasswordError(ValueError):
    pass


def validate_password_strength(password: str, email: str = "") -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    lowered = password.lower()
    if lowered in _COMMON_PASSWORDS or len(set(lowered)) < 5:
        raise WeakPasswordError("Password is too common or too repetitive")
    local = email.split("@")[0].lower()
    if local and len(local) >= 4 and local in lowered:
        raise WeakPasswordError("Password must not contain your email name")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# A real hash of a random password, computed once, so a login attempt for an
# unknown email burns the same CPU time as one for a real user (no timing oracle).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def verify_against_dummy(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- TOTP (RFC 6238) --------------------------------------------------------

def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded, casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_code(secret_b32: str, at: Optional[float] = None, step: int = 30) -> str:
    return _hotp(secret_b32, int((time.time() if at is None else at) // step))


def verify_totp(secret_b32: str, code: str, *, at: Optional[float] = None, step: int = 30, window: int = 1) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return False
    counter = int((time.time() if at is None else at) // step)
    return any(hmac.compare_digest(_hotp(secret_b32, counter + drift), code) for drift in range(-window, window + 1))


def totp_provisioning_uri(secret_b32: str, email: str, issuer: str = "SparseReST Company OS") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
