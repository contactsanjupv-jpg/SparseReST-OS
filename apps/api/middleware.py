"""
Cross-cutting HTTP middleware (pure ASGI, so SSE streaming is never buffered):
request IDs + structured request logging, security headers, request-size limit.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from packages.core.log import bind_log_context, get_logger, log

_log = get_logger("http")


def _header(scope: dict, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return None


class RequestContextMiddleware:
    """Assigns/propagates X-Request-ID, binds it into every log line emitted while
    handling the request, and writes one structured access-log line per request.
    Query strings are deliberately NOT logged (they can carry tokens)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        incoming = (_header(scope, b"x-request-id") or "").strip()
        request_id = incoming if 8 <= len(incoming) <= 64 and incoming.replace("-", "").isalnum() else uuid.uuid4().hex
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(b"x-request-id", request_id.encode())]
            await send(message)

        with bind_log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                path = scope.get("path", "")
                level = logging.DEBUG if path.startswith("/health") else (logging.WARNING if status_holder["status"] >= 500 else logging.INFO)
                client = scope.get("client")
                log(_log, level, "request", method=scope.get("method"), path=path, status=status_holder["status"],
                    duration_ms=round((time.perf_counter() - started) * 1000, 1), client=client[0] if client else None)


class SecurityHeadersMiddleware:
    """The API returns JSON/SSE only -- never HTML -- so a maximally strict CSP is safe."""

    def __init__(self, app, *, hsts: bool = False):
        self.app = app
        self._headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
            (b"cross-origin-resource-policy", b"same-site"),
            (b"permissions-policy", b"geolocation=(), camera=(), microphone=()"),
        ]
        if hsts:
            self._headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [h for h in self._headers if h[0] not in existing]
                if b"cache-control" not in existing:
                    extra.append((b"cache-control", b"no-store"))
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Rejects oversized request bodies -- by Content-Length up front, and by
    counting bytes for chunked uploads that don't declare one."""

    def __init__(self, app, *, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        declared = _header(scope, b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return await self._reject(send)

        received = 0
        started = {"value": False}

        async def receive_wrapper():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                started["value"] = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except _BodyTooLarge:
            if not started["value"]:
                await self._reject(send)

    @staticmethod
    async def _reject(send):
        body = json.dumps({"detail": "Request body too large"}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
