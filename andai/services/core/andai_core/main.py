"""andai-core: the FastAPI application.

`create_app()` builds the app; its services (database engine, login limiter, mailer, client-address
resolver) are created at startup from the environment unless a caller passes them in (tests).
Startup refuses to complete outside `ANDAI_ENV=dev` without a key-encryption key
(`crypto.load_keyring` raises `KeyUnavailable`), so a production service never runs without one.
A session in a two-factor stage reaches only its own routes (`auth.stage_gate`).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from andai_core.auth import routes as auth_routes
from andai_core.auth.clientip import ClientIpResolver
from andai_core.auth.csrf import OriginCheck
from andai_core.auth.deps import Services
from andai_core.auth.mfa import MFA_FAILURE_LIMIT, MFA_FAILURE_WINDOW_SECONDS
from andai_core.auth.ratelimit import LOGIN_LIMIT, LOGIN_WINDOW_SECONDS, ValkeyLimiter
from andai_core.auth.stage_gate import StageGate
from andai_core.crypto import load_keyring
from andai_core.db.session import create_app_engine
from andai_core.org import routes as org_routes
from andai_core.org import security as org_security
from andai_core.mail import SmtpMailer
from andai_core.settings import Settings, get_settings

SERVICE_NAME = "andai-core"


def build_services(settings: Settings) -> Services:
    """The production services. Raises `KeyUnavailable` outside dev without a KEK."""
    keyring = load_keyring(settings)
    client = redis.Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5)
    return Services(
        settings=settings,
        engine=create_app_engine(settings=settings),
        login_limiter=ValkeyLimiter(client, LOGIN_LIMIT, LOGIN_WINDOW_SECONDS),
        reset_limiter=ValkeyLimiter(client, LOGIN_LIMIT, LOGIN_WINDOW_SECONDS),
        mfa_limiter=ValkeyLimiter(client, MFA_FAILURE_LIMIT, MFA_FAILURE_WINDOW_SECONDS),
        mailer=SmtpMailer(settings),
        ip_resolver=ClientIpResolver(settings.trusted_proxies),
        keyring=keyring,
    )


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default body echoes the rejected input, which may be a password or a token.
    errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


async def _store_unavailable(_: Request, __: redis.RedisError) -> JSONResponse:
    return JSONResponse({"detail": "temporarily unavailable"}, status_code=503)


def create_app(services: Services | None = None) -> FastAPI:
    settings = services.settings if services is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        built = app.state.services is None
        if built:
            app.state.services = build_services(settings)
        try:
            yield
        finally:
            if built:
                app.state.services.engine.dispose()
                app.state.services = None

    app = FastAPI(
        title=SERVICE_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.services = services
    # Middleware wraps every route, including ones added later. The last added runs first: the
    # Origin check refuses cross-site writes before the stage gate looks up the session.
    app.add_middleware(StageGate)
    app.add_middleware(OriginCheck, allowed_origin=settings.portal_origin)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(redis.RedisError, _store_unavailable)
    app.include_router(auth_routes.router)
    app.include_router(org_security.router)
    app.include_router(org_routes.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {
            "service": SERVICE_NAME,
            "status": "ok",
            "version": os.environ.get("ANDAI_VERSION", "dev"),
        }

    return app


app = create_app()
