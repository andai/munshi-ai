"""Request-scoped auth plumbing: the app's services, the client address, the current session.

`current_session` turns the `andai_session` cookie into an `ActiveSession` (or 401). A route that
reads or writes org data opens its transaction with `org_transaction(services, auth)`, which sets
the row-level-security context to the session's org and user (`SET LOCAL app.org_id`,
`app.user_id`) for that transaction alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Tx

from andai_core.auth import sessions
from andai_core.auth.clientip import ClientIpResolver
from andai_core.auth.ratelimit import Limiter
from andai_core.db.session import transaction
from andai_core.mail import Mailer
from andai_core.settings import Settings


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Services:
    settings: Settings
    engine: Engine
    login_limiter: Limiter
    reset_limiter: Limiter
    mailer: Mailer
    ip_resolver: ClientIpResolver
    clock: Callable[[], datetime] = utcnow

    @property
    def lifetimes(self) -> sessions.Lifetimes:
        return sessions.Lifetimes.from_settings(self.settings)


NOT_AUTHENTICATED = HTTPException(status_code=401, detail="not authenticated")


def get_services(request: Request) -> Services:
    services = request.app.state.services
    if services is None:
        raise RuntimeError("andai-core services are not initialised")
    return services


def client_ip(request: Request, services: Services = Depends(get_services)) -> str | None:
    peer = request.client.host if request.client else None
    return services.ip_resolver.client_ip(peer, request.headers.get("x-forwarded-for"))


def current_session(
    request: Request, services: Services = Depends(get_services)
) -> sessions.ActiveSession:
    """Any live session, whatever its stage."""
    token = request.cookies.get(sessions.COOKIE_NAME)
    if not token:
        raise NOT_AUTHENTICATED
    with transaction(services.engine) as tx:
        active = sessions.resolve(tx, token, lifetimes=services.lifetimes, now=services.clock())
    if active is None:
        raise NOT_AUTHENTICATED
    return active


def full_session(
    active: sessions.ActiveSession = Depends(current_session),
) -> sessions.ActiveSession:
    """A completed login (stage `full`)."""
    if active.stage != sessions.FULL:
        raise NOT_AUTHENTICATED
    return active


@contextmanager
def org_transaction(services: Services, active: sessions.ActiveSession) -> Iterator[Tx]:
    with transaction(services.engine, org_id=active.org_id, user_id=active.user_id) as tx:
        yield tx
