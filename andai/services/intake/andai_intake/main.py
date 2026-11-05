"""andai-intake: the notetaker mailbox (INV-01, D-4, D-54).

* `GET /healthz` — liveness.
* The SMTP receiver (`smtp.py`), started by the lifespan on `ANDAI_INTAKE_SMTP_BIND` in the same
  event loop: it accepts mail for `ANDAI_INTAKE_ADDRESSES` only and stores each message in the
  andai database (`inbound_mail`, through `store.py`) before answering 250.
* The retention loop (`purge.py`): raw bytes go after `ANDAI_INTAKE_RAW_RETENTION_DAYS`, rows after
  `ANDAI_INTAKE_ROW_RETENTION_DAYS`.
* The processor (`processor.py`, every 2 s): sender check, sender, limits, parsing, `bot.autoJoin`
  and the invite state machine; scheduled meetings are written as the sender's org, and the
  verified sender gets a plain-text confirmation through the SMTP relay (`confirm.py`, `SMTP_*`).
* The horizon top-up (`horizon.py`, hourly): occurrences of recurring invites that come within
  `ANDAI_INTAKE_HORIZON_DAYS`.

Without `ANDAI_DB_URL_APP` none of them starts (health only): a receiver that cannot store would
only answer 451. The service connects as the RLS-bound `andai_app` and touches `inbound_mail` only
through the migration 0012 functions. The compose overlay publishes both ports on 127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import redis
from fastapi import FastAPI
from sqlalchemy import Engine

from andai_core.db.session import create_app_engine
from andai_core.mail import SmtpMailer
from andai_intake import confirm, horizon, processor, purge, store
from andai_intake.auth.verdict import Policy
from andai_intake.limits import IntakeLimits
from andai_intake.settings import IntakeSettings, get_intake_settings
from andai_intake.smtp import IntakeHandler, start_smtp

SERVICE_NAME = "andai-intake"

log = logging.getLogger(__name__)


@dataclass
class IntakeServices:
    settings: IntakeSettings
    engine: Engine


def build_services(settings: IntakeSettings) -> IntakeServices | None:
    """The production services, or None without `ANDAI_DB_URL_APP` (health only)."""
    if settings.db_url_app is None:
        log.warning("ANDAI_DB_URL_APP is not set: no SMTP receiver, processor or purge")
        return None
    return IntakeServices(settings=settings, engine=create_app_engine(settings=settings))


async def start(services: IntakeServices) -> tuple[asyncio.Server, list[asyncio.Task]]:
    settings = services.settings
    if not settings.intake_addresses:
        log.warning("ANDAI_INTAKE_ADDRESSES is empty: every recipient is refused")
    handler = IntakeHandler(
        functools.partial(store.save, services.engine), settings.intake_addresses
    )
    host, port = settings.smtp_listen
    server = await start_smtp(handler, host, port, data_size_limit=settings.intake_max_bytes)
    log.info(
        "SMTP receiver on %s:%d for %d address(es), at most %d bytes",
        host,
        port,
        len(settings.intake_addresses),
        settings.intake_max_bytes,
    )
    purger = purge.PurgeRunner(
        services.engine,
        raw_days=settings.intake_raw_retention_days,
        row_days=settings.intake_row_retention_days,
    )
    confirmer = confirm.Confirmer(
        SmtpMailer(settings),
        confirm.valkey_limiter(
            redis.Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5)
        ),
        lead_s=settings.intake_join_lead_s,
        horizon_days=settings.intake_horizon_days,
        notetakers=settings.intake_addresses,
    )
    runner = processor.Processor(
        services.engine,
        settings,
        IntakeLimits.from_settings(settings),
        policy=Policy.from_settings(settings),
        horizon=settings.horizon,
        confirmer=confirmer,
    )
    top_up = horizon.HorizonRunner(
        services.engine, language=settings.default_language, horizon=settings.horizon
    )
    return server, [
        asyncio.create_task(purger.run(purge.TICK_S)),
        asyncio.create_task(runner.run(processor.TICK_S)),
        asyncio.create_task(top_up.run(horizon.TICK_S)),
    ]


def create_app(services: IntakeServices | None = None, *, run_jobs: bool = True) -> FastAPI:
    """The app; with `services` given (tests) nothing is built from the environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        built = app.state.services is None
        if built:
            app.state.services = build_services(get_intake_settings())
        current = app.state.services
        server, tasks = (None, [])
        if current is not None and run_jobs:
            server, tasks = await start(current)
        try:
            yield
        finally:
            if server is not None:
                server.close()  # stop listening; open sessions get a few seconds to finish
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(server.wait_closed(), timeout=5)
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

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {
            "service": SERVICE_NAME,
            "status": "ok",
            "version": os.environ.get("ANDAI_VERSION", "dev"),
        }

    return app


app = create_app()
