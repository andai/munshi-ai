"""andai-worker: canary joins, join metrics and operator alerts (BOT-04, D-34).

* `GET /healthz` — liveness.
* `GET /metrics` — join metrics in Prometheus text format (`metrics.py`), read from the andai
  database on every scrape; 503 while the worker has no database URL.
* Background loops, started by the lifespan:
  - canaries (`canary.py`) when at least one `ANDAI_CANARY_*_URL` is set;
  - alerts (`alerts.py`) when `ANDAI_ALERT_EMAIL` is set.

The worker uses andai-core's own code (shared uv workspace): its settings class, the RLS-bound
`andai_app` engine, the keyring (the canary org's platform key is sealed under `ANDAI_KEK`), the
gateway client, `Meetings` (the product's dispatch path) and the SMTP mailer. Nothing listens
beyond 127.0.0.1 on the host (compose overlay).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry
from sqlalchemy import Engine

from andai_core.crypto import load_keyring
from andai_core.db.session import create_app_engine
from andai_core.mail import Mailer, SmtpMailer
from andai_core.meetings.service import Meetings
from andai_core.platform.gateway_client import HttpPlatformGateway
from andai_worker import alerts, canary, metrics
from andai_worker.settings import WorkerSettings, get_worker_settings

SERVICE_NAME = "andai-worker"

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class WorkerServices:
    settings: WorkerSettings
    engine: Engine
    meetings: Meetings
    mailer: Mailer
    clock: Callable[[], datetime] = utcnow


def build_services(settings: WorkerSettings) -> WorkerServices | None:
    """The production services, or None without `ANDAI_DB_URL_APP` (health only)."""
    if settings.db_url_app is None:
        log.warning("ANDAI_DB_URL_APP is not set: no metrics, canaries or alerts")
        return None
    engine = create_app_engine(settings=settings)
    meetings = Meetings(
        engine,
        load_keyring(settings),
        HttpPlatformGateway(settings.platform_gateway_url),
        settings,
        utcnow,
    )
    return WorkerServices(
        settings=settings, engine=engine, meetings=meetings, mailer=SmtpMailer(settings)
    )


def jobs(services: WorkerServices) -> list[asyncio.Task]:
    settings = services.settings
    tasks = []
    urls = settings.canary_urls
    if urls:
        runner = canary.CanaryRunner(
            services.engine,
            services.meetings,
            urls,
            interval_s=settings.canary_interval_s,
            clock=services.clock,
        )
        every = canary.tick_seconds(settings.canary_interval_s)
        tasks.append(asyncio.create_task(runner.run(every)))
        log.info("canaries on %s every %s s", ", ".join(sorted(urls)), settings.canary_interval_s)
    else:
        log.info("no canary links configured: canaries off")
    if settings.alert_to:
        evaluator = alerts.AlertEvaluator(
            services.engine, services.mailer, settings.alert_to, clock=services.clock
        )
        every = min(alerts.TICK_S, canary.tick_seconds(settings.canary_interval_s))
        tasks.append(asyncio.create_task(evaluator.run(every)))
    else:
        log.info("ANDAI_ALERT_EMAIL is empty: alert mail off")
    return tasks


def create_app(services: WorkerServices | None = None, *, run_jobs: bool = True) -> FastAPI:
    """The app; with `services` given (tests) nothing is built from the environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        built = app.state.services is None
        if built:
            app.state.services = build_services(get_worker_settings())
        current = app.state.services
        if current is not None:
            app.state.registry = metrics.registry_for(current.engine, current.clock)
        tasks = jobs(current) if current is not None and run_jobs else []
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if built and app.state.services is not None:
                app.state.services.engine.dispose()
                app.state.services = None

    app = FastAPI(
        title=SERVICE_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.services = services
    app.state.registry = None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {
            "service": SERVICE_NAME,
            "status": "ok",
            "version": os.environ.get("ANDAI_VERSION", "dev"),
        }

    @app.get("/metrics")
    def scrape() -> Response:
        registry: CollectorRegistry | None = app.state.registry
        if registry is None:
            return Response("no database configured\n", status_code=503, media_type="text/plain")
        return Response(metrics.render(registry), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
