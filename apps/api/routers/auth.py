from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from apps.api.auth import _bearer, resolve_identity
from apps.api.auth_service import AccountLocked, AuthError, AuthService, Identity
from apps.api.security import WeakPasswordError
from packages.core.config import get_settings

router = APIRouter()


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=512)
    totp_code: Optional[str] = Field(default=None, max_length=12)


class LoginOut(BaseModel):
    token: str
    expires_at: str
    email: str
    totp_enabled: bool


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=512)
    new_password: str = Field(max_length=512)


class TotpEnableIn(BaseModel):
    secret: str = Field(max_length=64)
    code: str = Field(max_length=12)


class TotpDisableIn(BaseModel):
    password: str = Field(max_length=512)
    code: str = Field(max_length=12)


def _raise(exc: AuthError) -> None:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers)


def _session_user(authorization: Optional[str] = Header(default=None)) -> Identity:
    identity = resolve_identity(authorization)
    if identity is None or identity.user_id is None:
        raise HTTPException(status_code=403, detail="This action needs a signed-in founder account (API keys and open dev mode can't do it)")
    return identity


@router.get("/status")
def auth_status() -> dict:
    """Unauthenticated, reveals only whether the UI should show a login form."""
    settings = get_settings()
    login_required = bool(settings.founder_api_key) or AuthService.any_account() or settings.is_production
    return {"login_required": login_required, "accounts_exist": AuthService.any_account()}


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, request: Request) -> LoginOut:
    try:
        result = AuthService.login(
            body.email, body.password, body.totp_code,
            ip=request.client.host if request.client else None, user_agent=request.headers.get("user-agent"),
        )
    except AuthError as exc:
        _raise(exc)
    return LoginOut(token=result.token, expires_at=result.expires_at.isoformat(), email=result.identity.email, totp_enabled=result.identity.totp_enabled)


@router.post("/logout", status_code=204)
def logout(authorization: Optional[str] = Header(default=None)) -> Response:
    token = _bearer(authorization)
    if token:
        AuthService.logout(token)
    return Response(status_code=204)


@router.get("/me")
def me(authorization: Optional[str] = Header(default=None)) -> dict:
    identity = resolve_identity(authorization)
    return {"email": identity.email, "via": identity.via, "totp_enabled": identity.totp_enabled}


@router.post("/change-password", status_code=204)
def change_password(body: ChangePasswordIn, authorization: Optional[str] = Header(default=None),
                    identity: Identity = Depends(_session_user)) -> Response:
    try:
        AuthService.change_password(identity.user_id, body.current_password, body.new_password, keep_token=_bearer(authorization))
    except AuthError as exc:
        _raise(exc)
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return Response(status_code=204)


@router.post("/totp/setup")
def totp_setup(identity: Identity = Depends(_session_user)) -> dict:
    secret, uri = AuthService.begin_totp_setup(identity.user_id)
    return {"secret": secret, "otpauth_uri": uri}


@router.post("/totp/enable", status_code=204)
def totp_enable(body: TotpEnableIn, identity: Identity = Depends(_session_user)) -> Response:
    try:
        AuthService.enable_totp(identity.user_id, body.secret, body.code)
    except AuthError as exc:
        _raise(exc)
    return Response(status_code=204)


@router.post("/totp/disable", status_code=204)
def totp_disable(body: TotpDisableIn, identity: Identity = Depends(_session_user)) -> Response:
    try:
        AuthService.disable_totp(identity.user_id, body.password, body.code)
    except AuthError as exc:
        _raise(exc)
    return Response(status_code=204)
