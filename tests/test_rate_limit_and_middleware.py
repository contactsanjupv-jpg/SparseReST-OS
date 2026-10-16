"""Rate limiting, request IDs/logging, security headers, body-size limit, CORS, trusted hosts."""
from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware, SecurityHeadersMiddleware
from apps.api.ratelimit import MAX_TRACKED_CLIENTS, RateLimitMiddleware, TokenBucketLimiter, client_key
from packages.core.config import get_settings
from packages.core.log import configure_logging


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def _settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---- the bucket itself -----------------------------------------------------------------------

def test_bucket_allows_burst_then_blocks_then_refills():
    clock = FakeClock()
    limiter = TokenBucketLimiter(60, clock=clock)      # 1 token/second, burst 60
    assert all(limiter.check("a")[0] for _ in range(60))
    allowed, retry_after = limiter.check("a")
    assert not allowed and 0 < retry_after <= 1.0
    clock.advance(1.0)
    assert limiter.check("a")[0] and not limiter.check("a")[0]


def test_buckets_are_per_client():
    limiter = TokenBucketLimiter(2, clock=FakeClock())
    assert limiter.check("a")[0] and limiter.check("a")[0] and not limiter.check("a")[0]
    assert limiter.check("b")[0]


def test_bucket_memory_is_bounded_under_a_many_client_flood():
    clock = FakeClock()
    limiter = TokenBucketLimiter(10, clock=clock)
    for i in range(MAX_TRACKED_CLIENTS + 500):
        limiter.check(f"attacker-{i}")
    assert len(limiter._buckets) <= MAX_TRACKED_CLIENTS


# ---- client identity --------------------------------------------------------------------------

def _scope(xff: str | None = None, peer="10.0.0.9"):
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return {"type": "http", "headers": headers, "client": (peer, 1234)}


def test_forwarded_header_ignored_unless_trusted():
    assert client_key(_scope("1.2.3.4"), trust_proxy_headers=False) == "10.0.0.9"


def test_trusted_proxy_uses_last_entry_not_the_spoofable_first():
    assert client_key(_scope("6.6.6.6, 1.2.3.4"), trust_proxy_headers=True) == "1.2.3.4"
    assert client_key(_scope(None), trust_proxy_headers=True) == "10.0.0.9"


# ---- middleware on a tiny app ------------------------------------------------------------------

def _mini(per_minute=3, login_per_minute=2, **kw) -> TestClient:
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/auth/login")
    def login():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, per_minute=per_minute, login_per_minute=login_per_minute, clock=FakeClock(), **kw)
    return TestClient(app)


def test_middleware_returns_429_with_retry_after():
    c = _mini()
    assert [c.get("/ping").status_code for _ in range(3)] == [200, 200, 200]
    r = c.get("/ping")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_health_is_exempt_from_limiting():
    c = _mini(per_minute=1)
    assert all(c.get("/health").status_code == 200 for _ in range(20))


def test_login_has_its_own_stricter_bucket():
    c = _mini(per_minute=100, login_per_minute=2)
    assert [c.post("/auth/login").status_code for _ in range(3)] == [200, 200, 429]
    assert c.get("/ping").status_code == 200        # general traffic unaffected by login throttling


def test_limiter_can_be_disabled():
    c = _mini(per_minute=1, enabled=False)
    assert all(c.get("/ping").status_code == 200 for _ in range(10))


def test_spoofed_forwarded_header_cannot_dodge_the_limit_by_default():
    c = _mini(per_minute=2)
    statuses = [c.get("/ping", headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code for i in range(4)]
    assert statuses == [200, 200, 429, 429]


# ---- request context / logging ----------------------------------------------------------------------

def test_request_id_generated_propagated_and_logged():
    stream = io.StringIO()
    configure_logging("test", level="DEBUG", fmt="json", stream=stream)
    app = FastAPI()

    @app.get("/x")
    def x():
        return {}

    app.add_middleware(RequestContextMiddleware)
    c = TestClient(app)

    r = c.get("/x?token=supersecret")
    rid = r.headers["x-request-id"]
    assert len(rid) >= 8
    lines = [json.loads(l) for l in stream.getvalue().splitlines() if '"request"' in l]
    assert lines[-1]["request_id"] == rid and lines[-1]["path"] == "/x" and lines[-1]["status"] == 200
    assert "supersecret" not in stream.getvalue()           # query strings are never logged

    assert c.get("/x", headers={"X-Request-ID": "abcdef1234567890"}).headers["x-request-id"] == "abcdef1234567890"
    assert c.get("/x", headers={"X-Request-ID": "bad id!\n"}).headers["x-request-id"] != "bad id!\n"    # hostile IDs replaced


def test_5xx_is_logged_at_warning_or_above():
    stream = io.StringIO()
    configure_logging("test", level="DEBUG", fmt="json", stream=stream)
    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    app.add_middleware(RequestContextMiddleware)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/boom").status_code == 500
    record = [json.loads(l) for l in stream.getvalue().splitlines() if '"request"' in l][-1]
    assert record["status"] == 500 and record["level"] == "WARNING"


# ---- security headers / body limit ------------------------------------------------------------------------

def test_security_headers_present_and_hsts_only_when_asked():
    def app_with(hsts):
        app = FastAPI()

        @app.get("/x")
        def x():
            return {}

        app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
        return TestClient(app)

    h = app_with(False).get("/x").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert "default-src 'none'" in h["content-security-policy"] and h["cache-control"] == "no-store"
    assert "strict-transport-security" not in h
    assert "max-age=31536000" in app_with(True).get("/x").headers["strict-transport-security"]


def test_body_limit_rejects_by_content_length_and_by_streaming():
    app = FastAPI()

    @app.post("/up")
    async def up(request: Request):
        return {"n": len(await request.body())}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=100)
    c = TestClient(app)
    assert c.post("/up", content=b"x" * 50).json() == {"n": 50}
    assert c.post("/up", content=b"x" * 500).status_code == 413

    def chunks():                                             # no Content-Length: chunked transfer
        for _ in range(10):
            yield b"y" * 30
    assert c.post("/up", content=chunks()).status_code == 413


# ---- the real app's edge ----------------------------------------------------------------------------------

def test_real_app_cors_only_allows_configured_origin(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.sparserest.test")
    get_settings.cache_clear()
    c = TestClient(create_app())
    good = c.options("/tasks", headers={"Origin": "https://app.sparserest.test", "Access-Control-Request-Method": "GET"})
    bad = c.options("/tasks", headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "GET"})
    assert good.headers.get("access-control-allow-origin") == "https://app.sparserest.test"
    assert "access-control-allow-origin" not in bad.headers
    assert good.headers.get("access-control-allow-credentials") is None       # bearer auth, never cookies


def test_real_app_rejects_unknown_hosts_when_configured(monkeypatch):
    monkeypatch.setenv("TRUSTED_HOSTS", "api.sparserest.test")
    get_settings.cache_clear()
    c = TestClient(create_app(), base_url="http://evil.test")
    assert c.get("/health/live").status_code == 400
    ok = TestClient(create_app(), base_url="http://api.sparserest.test")
    assert ok.get("/health/live").status_code == 200


def test_real_app_sets_headers_and_request_id_end_to_end():
    r = TestClient(create_app()).get("/health/live")
    assert r.status_code == 200 and r.headers["x-request-id"] and r.headers["x-frame-options"] == "DENY"


def test_ready_reports_schema_state():
    r = TestClient(create_app()).get("/health/ready")
    assert r.status_code == 200 and r.json()["schema"]["up_to_date"] is True
