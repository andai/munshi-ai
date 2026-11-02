"""Cross-site request refusal.

Authentication is a SameSite=Lax cookie, and the portal proxies `/api/*` from its own origin, so
every legitimate state-changing request carries `Origin: <ANDAI_PORTAL_ORIGIN>`. A POST, PUT,
PATCH or DELETE whose `Origin` is missing or different is refused with 403 before it reaches a
route. Safe methods (GET, HEAD, OPTIONS) pass.

One path is exempt, matched exactly: the platform's system webhook (`/internal/platform-events`,
`platform/webhook.py`). It is server-to-server, carries no cookie and is authenticated by its HMAC
signature; no prefix or other path is exempt.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Signature-authenticated, cookie-less server-to-server paths (exact matches only); the stage gate
# leaves them alone too.
SIGNED_PATHS = frozenset({"/internal/platform-events"})


def normalize_origin(origin: str) -> str:
    """`scheme://host[:port]`, lowercased, default ports dropped; '' if it is not an origin."""
    parts = urlsplit(origin.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    default = {"http": 80, "https": 443}[parts.scheme]
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + ("" if port in (None, default) else f":{port}")


class OriginCheck:
    def __init__(self, app: ASGIApp, *, allowed_origin: str) -> None:
        self.app = app
        self.allowed = normalize_origin(allowed_origin)
        if not self.allowed:
            raise ValueError("ANDAI_PORTAL_ORIGIN is not a valid origin")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"].upper() in STATE_CHANGING
            and scope["path"] not in SIGNED_PATHS
        ):
            origin = next(
                (v.decode("latin-1") for k, v in scope["headers"] if k == b"origin"), None
            )
            if origin is None or normalize_origin(origin) != self.allowed:
                body = json.dumps({"detail": "cross-site request refused"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
