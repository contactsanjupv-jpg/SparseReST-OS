from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from apps.api.auth import require_founder_key
from apps.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware, SecurityHeadersMiddleware
from apps.api.ratelimit import RateLimitMiddleware
from apps.api.routers import agents, analytics, approvals, auth, autopilot, health, incidents, ops, products, tasks, world
from packages.core.config import get_settings
from packages.core.log import configure_logging, get_logger


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging("api")

    # Interactive API docs list every route and schema -- useful in dev, free reconnaissance in production.
    docs_kwargs = {"docs_url": None, "redoc_url": None, "openapi_url": None} if settings.is_production else {}
    app = FastAPI(title="SparseReST Company OS -- Control Plane", version="1.0.0", **docs_kwargs)

    protected = [Depends(require_founder_key)]
    app.include_router(health.router)
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(tasks.router, prefix="/tasks", tags=["tasks"], dependencies=protected)
    app.include_router(approvals.router, prefix="/approvals", tags=["approvals"], dependencies=protected)
    app.include_router(agents.router, prefix="/agents", tags=["agents"], dependencies=protected)
    app.include_router(incidents.router, prefix="/incidents", tags=["incidents"], dependencies=protected)
    app.include_router(products.router, prefix="/products", tags=["products"], dependencies=protected)
    app.include_router(analytics.router, prefix="/analytics", tags=["analytics"], dependencies=protected)
    app.include_router(ops.router, prefix="/ops", tags=["ops"], dependencies=protected)
    app.include_router(autopilot.router, prefix="/autopilot", tags=["autopilot"], dependencies=protected)
    app.include_router(world.router, prefix="/world", tags=["world"], dependencies=protected)

    # Middleware is applied bottom-up: the LAST one added is the OUTERMOST. Order matters:
    #   request context (ids + logging) -> security headers -> body limit -> rate limit -> CORS -> routes
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,                     # auth is a bearer header, never a cookie -- so no CSRF surface
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )
    app.add_middleware(
        RateLimitMiddleware, per_minute=settings.rate_limit_per_minute,
        login_per_minute=settings.login_rate_limit_per_minute,
        enabled=settings.rate_limit_enabled, trust_proxy_headers=settings.trust_proxy_headers,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    if settings.trusted_host_list != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
    app.add_middleware(RequestContextMiddleware)

    get_logger("api").info("api started", extra={"fields": {"environment": settings.environment, "docs": not settings.is_production}})
    return app


app = create_app()
