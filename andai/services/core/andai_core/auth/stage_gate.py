"""The two-factor stage gate.

A request that carries a live session in a two-factor stage reaches only that stage's own routes:

* `mfa_pending` (password right, code not yet entered): `/api/v1/auth/mfa/verify` and
  `/api/v1/auth/logout`;
* `enroll_required` (the org requires 2FA and the user has not set it up): `/api/v1/me/mfa`,
  everything under `/api/v1/me/mfa/`, and `/api/v1/auth/logout`.

Every other path answers 401 with `{"next_step": "mfa_verify" | "mfa_enroll"}` before any route
runs, so a route added later is gated without opting in. Requests without a session cookie, with a
dead one, or with a `full` session pass through unchanged; the routes do their own authentication.
"""

from __future__ import annotations

import json

import anyio.to_thread
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from andai_core.auth import sessions
from andai_core.db.session import transaction

LOGOUT = "/api/v1/auth/logout"
MFA_VERIFY = "/api/v1/auth/mfa/verify"
MFA_ENROLL_PREFIX = "/api/v1/me/mfa"

NEXT_STEP = {sessions.MFA_PENDING: "mfa_verify", sessions.ENROLL_REQUIRED: "mfa_enroll"}
DETAIL = {
    sessions.MFA_PENDING: "two-factor verification required",
    sessions.ENROLL_REQUIRED: "two-factor enrollment required",
}


def allowed(stage: str, path: str) -> bool:
    """Whether a session in `stage` may reach `path`."""
    if stage == sessions.FULL:
        return True
    if path == LOGOUT:
        return True
    if stage == sessions.MFA_PENDING:
        return path == MFA_VERIFY
    if stage == sessions.ENROLL_REQUIRED:
        return path == MFA_ENROLL_PREFIX or path.startswith(MFA_ENROLL_PREFIX + "/")
    return False


class StageGate:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = HTTPConnection(scope).cookies.get(sessions.COOKIE_NAME)
        stage = await anyio.to_thread.run_sync(self._stage, scope, token) if token else None
        if stage is None or allowed(stage, scope["path"]):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps(
            {
                "detail": DETAIL.get(stage, "not authenticated"),
                "next_step": NEXT_STEP.get(stage, "login"),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _stage(scope: Scope, token: str) -> str | None:
        services = scope["app"].state.services
        with transaction(services.engine) as tx:
            active = sessions.resolve(
                tx, token, lifetimes=services.lifetimes, now=services.clock()
            )
        return None if active is None else active.stage
