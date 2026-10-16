"""
API authentication dependency.

Two ways in, both checked here:
  1. A founder SESSION token (from POST /auth/login) -- what the web app uses.
  2. The static FOUNDER_API_KEY -- a service credential for scripts/CI/curl.
     It is compared in constant time and, like every secret, may be supplied as a
     secret:// reference so it never sits in .env.

Open mode -- when NO credential mechanism is configured (no API key AND no
founder account) and the environment is not production -- exists only so a fresh
local checkout is usable at http://localhost. In production the API fails CLOSED:
with nothing configured, every protected route answers 503 rather than silently
serving the company's data to anyone who can reach the port.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Header, HTTPException

from apps.api.auth_service import AuthService, Identity
from packages.core.config import get_settings


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def resolve_identity(authorization: Optional[str]) -> Optional[Identity]:
    """Returns who is calling, or raises HTTPException. Never returns None for a
    caller that should be rejected."""
    settings = get_settings()
    token = _bearer(authorization)

    if token and settings.founder_api_key and hmac.compare_digest(token, settings.founder_api_key):
        return Identity(None, "api-key", via="api_key")

    if token:
        identity = AuthService.validate_token(token)
        if identity is not None:
            return identity

    auth_configured = bool(settings.founder_api_key) or AuthService.any_account()
    if not auth_configured:
        if settings.is_production and settings.require_login_in_production:
            raise HTTPException(status_code=503, detail="Authentication is not configured. Create a founder account (scripts/create_founder.py) before exposing this API.")
        return Identity(None, "open-dev-mode", via="open")

    raise HTTPException(status_code=401, detail="Missing or invalid credentials", headers={"WWW-Authenticate": "Bearer"})


def require_founder_key(authorization: str | None = Header(default=None)) -> Optional[Identity]:
    """Router-level dependency (name kept for backward compatibility)."""
    return resolve_identity(authorization)
