"""
Rate limiting without Redis: an in-process token bucket per client.

Scope, stated plainly: limits are enforced PER API PROCESS. Running N uvicorn
workers therefore allows up to N x the configured rate per client. That is
acceptable for this system's threat model (a single-founder control plane) because
the security-critical control -- brute-force resistance on login -- does NOT
depend on this module: account lockout is stored in Postgres and is shared by every
process (apps/api/auth_service.py). This limiter is the cheap first line that stops
floods and accidental request loops before they reach the database.

Client identity is the socket peer address. `X-Forwarded-For` is honoured ONLY when
TRUST_PROXY_HEADERS is enabled (you run behind your own reverse proxy), and then only
the LAST entry -- the one your proxy appended -- is used, since earlier entries are
client-controlled and trivially spoofed.
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

EXEMPT_PREFIXES = ("/health",)
MAX_TRACKED_CLIENTS = 10_000


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    def __init__(self, per_minute: int, *, burst: Optional[int] = None, clock: Callable[[], float] = time.monotonic):
        self.rate = per_minute / 60.0
        self.capacity = float(burst if burst is not None else per_minute)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= MAX_TRACKED_CLIENTS:
                    self._prune(now)
                bucket = self._buckets[key] = _Bucket(self.capacity, now)
            bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.rate)
            bucket.updated = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            return False, (1.0 - bucket.tokens) / self.rate if self.rate > 0 else 60.0

    def _prune(self, now: float) -> None:
        idle_after = self.capacity / self.rate if self.rate > 0 else 600.0   # a full refill: nothing to remember
        for key in [k for k, b in self._buckets.items() if now - b.updated >= idle_after]:
            del self._buckets[key]
        if len(self._buckets) >= MAX_TRACKED_CLIENTS:                         # still full (attack): drop oldest half
            for key in sorted(self._buckets, key=lambda k: self._buckets[k].updated)[: MAX_TRACKED_CLIENTS // 2]:
                del self._buckets[key]


def client_key(scope: dict, *, trust_proxy_headers: bool) -> str:
    if trust_proxy_headers:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                parts = [p.strip() for p in value.decode("latin-1").split(",") if p.strip()]
                if parts:
                    return parts[-1]
    client = scope.get("client")
    return client[0] if client else "unknown"


class RateLimitMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so streaming/SSE responses are untouched."""

    def __init__(self, app, *, per_minute: int, login_per_minute: int = 10, enabled: bool = True,
                 trust_proxy_headers: bool = False, clock: Callable[[], float] = time.monotonic):
        self.app = app
        self.enabled = enabled
        self.trust_proxy_headers = trust_proxy_headers
        self.general = TokenBucketLimiter(per_minute, clock=clock)
        self.login = TokenBucketLimiter(login_per_minute, clock=clock)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path.startswith(EXEMPT_PREFIXES) or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)

        key = client_key(scope, trust_proxy_headers=self.trust_proxy_headers)
        limiter = self.login if (path == "/auth/login" and scope.get("method") == "POST") else self.general
        bucket_key = f"login:{key}" if limiter is self.login else key
        allowed, retry_after = limiter.check(bucket_key)
        if allowed:
            return await self.app(scope, receive, send)

        body = json.dumps({"detail": "Rate limit exceeded. Slow down and retry."}).encode()
        await send({"type": "http.response.start", "status": 429, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"retry-after", str(max(1, math.ceil(retry_after))).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})
