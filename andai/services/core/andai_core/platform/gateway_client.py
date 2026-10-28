"""A client for the meeting platform's gateway, called with one org's own API key.

Every call carries `X-API-Key: <the org's key>` to `PLATFORM_GATEWAY_URL` (the stack's `gateway`
service on the private network). The key is passed per call, unsealed by the caller inside
andai-core for that call only; this module never stores, logs or repeats it.

* `dispatch(key, body)` — `POST /bots`; answers the platform's MeetingResponse.
* `list_bots(key)` — `GET /bots`; the principal's meetings, newest and still-running first.
* `get_meeting(key, id)` — `GET /meetings/{id}`.
* `stop(key, platform, native_meeting_id)` — `DELETE /bots/{platform}/{native_meeting_id}`.

Errors carry the call and the platform's status code only — never the key, a request body or a
response body. A duplicate dispatch is `DuplicateMeeting`, carrying the ids of the meeting the
platform already runs for that link (its structured 409, `code: duplicate_meeting`).
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import quote

import httpx

API_KEY_HEADER = "X-API-Key"
TIMEOUT_SECONDS = 10.0
# Spawning a bot starts a container before `POST /bots` answers.
DISPATCH_TIMEOUT_SECONDS = 60.0
LIST_LIMIT = 100


class GatewayError(Exception):
    """The gateway could not be reached (`status` None) or answered an error status."""

    def __init__(self, what: str, status: int | None, detail: str | None = None) -> None:
        self.what = what
        self.status = status
        message = (
            f"gateway {what}: {detail}" if status is None else f"gateway {what} answered {status}"
        )
        super().__init__(message)


class DuplicateMeeting(GatewayError):
    """The platform already runs a bot for this meeting under this org's principal."""

    def __init__(
        self, meeting_id: int | None, platform: str | None, native_meeting_id: str | None
    ) -> None:
        super().__init__("dispatch", 409)
        self.meeting_id = meeting_id
        self.platform = platform
        self.native_meeting_id = native_meeting_id


class PlatformGateway(Protocol):
    async def dispatch(self, key: str, body: dict) -> dict: ...

    async def list_bots(self, key: str, *, limit: int = LIST_LIMIT) -> list[dict]: ...

    async def get_meeting(self, key: str, meeting_id: int) -> dict: ...

    async def stop(self, key: str, platform: str, native_meeting_id: str) -> None: ...


def _object(what: str, response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        raise GatewayError(what, None, "non-JSON body") from None
    if not isinstance(body, dict):
        raise GatewayError(what, None, "unexpected body")
    return body


def _duplicate(response: httpx.Response) -> DuplicateMeeting | None:
    """The structured duplicate 409, or None for any other 409."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("code") != "duplicate_meeting":
        return None
    meeting_id = body.get("meeting_id")
    platform = body.get("platform")
    native = body.get("native_meeting_id")
    return DuplicateMeeting(
        meeting_id if isinstance(meeting_id, int) and not isinstance(meeting_id, bool) else None,
        platform if isinstance(platform, str) else None,
        native if isinstance(native, str) else None,
    )


class HttpPlatformGateway:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
        dispatch_timeout: float = DISPATCH_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout
        self._dispatch_timeout = dispatch_timeout

    async def _call(
        self, what: str, key: str, method: str, path: str, *, timeout: float | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        if not key:
            raise GatewayError(what, None, "no API key")
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers={API_KEY_HEADER: key},
                transport=self._transport,
                timeout=timeout or self._timeout,
            ) as client:
                return await client.request(method, path, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise GatewayError(what, None, type(exc).__name__) from None

    async def dispatch(self, key: str, body: dict) -> dict:
        response = await self._call(
            "dispatch", key, "POST", "/bots", json=body, timeout=self._dispatch_timeout
        )
        if response.status_code == 409:
            duplicate = _duplicate(response)
            if duplicate is not None:
                raise duplicate
        if response.status_code >= 400:
            raise GatewayError("dispatch", response.status_code)
        return _object("dispatch", response)

    async def list_bots(self, key: str, *, limit: int = LIST_LIMIT) -> list[dict]:
        response = await self._call("list bots", key, "GET", "/bots", params={"limit": limit})
        if response.status_code >= 400:
            raise GatewayError("list bots", response.status_code)
        meetings = _object("list bots", response).get("meetings")
        if not isinstance(meetings, list):
            raise GatewayError("list bots", None, "no meetings list")
        return [m for m in meetings if isinstance(m, dict)]

    async def get_meeting(self, key: str, meeting_id: int) -> dict:
        response = await self._call("get meeting", key, "GET", f"/meetings/{int(meeting_id)}")
        if response.status_code >= 400:
            raise GatewayError("get meeting", response.status_code)
        return _object("get meeting", response)

    async def stop(self, key: str, platform: str, native_meeting_id: str) -> None:
        path = f"/bots/{quote(platform, safe='')}/{quote(native_meeting_id, safe='')}"
        response = await self._call("stop", key, "DELETE", path)
        if response.status_code >= 400:
            raise GatewayError("stop", response.status_code)
