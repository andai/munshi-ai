"""A client for the meeting platform's admin-api (the admin tier: users and their API tokens).

Every call carries `X-Admin-API-Key: PLATFORM_ADMIN_TOKEN` to `PLATFORM_ADMIN_URL` (the stack's
`admin-api` service on the private network). Two calls make a principal:

* `POST /admin/users {"email"}` — creates the user, or answers 200 with the existing one for that
  email, so a retry finds the same user;
* `POST /admin/users/{id}/tokens?scopes=bot,tx` — mints a new API token (tokens are additive);
  `DELETE /admin/tokens/{id}` revokes one.

`PATCH /admin/users/{id} {"data", "max_concurrent_bots"}` sets the principal's platform settings
(its storage prefix, diagnostics switches and bot cap); the admin-api merges `data` into the user's
record, so applying the same patch twice changes nothing.

Errors say which call failed and its status, never a token, the admin key or a response body.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from pydantic import SecretStr

from andai_core.settings import Settings

ADMIN_KEY_HEADER = "X-Admin-API-Key"
TIMEOUT_SECONDS = 10.0


class PlatformAdminError(Exception):
    """The admin-api could not be reached or refused the call."""


@dataclass(frozen=True)
class MintedToken:
    id: int
    token: str = field(repr=False)


class PlatformAdmin(Protocol):
    def ensure_user(self, email: str) -> int: ...

    def mint_token(self, user_id: int, scopes: Sequence[str]) -> MintedToken: ...

    def revoke_token(self, token_id: int) -> None: ...

    def update_user(self, user_id: int, *, data: dict, max_concurrent_bots: int) -> None: ...


class HttpPlatformAdmin:
    def __init__(
        self,
        base_url: str,
        admin_token: SecretStr | None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._admin_token = admin_token
        self._transport = transport
        self._timeout = timeout

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.BaseTransport | None = None
    ) -> HttpPlatformAdmin:
        return cls(
            settings.platform_admin_url, settings.platform_admin_token, transport=transport
        )

    def _call(self, what: str, method: str, path: str, **kwargs: object) -> dict:
        if self._admin_token is None or not self._admin_token.get_secret_value():
            raise PlatformAdminError("PLATFORM_ADMIN_TOKEN is not set")
        headers = {ADMIN_KEY_HEADER: self._admin_token.get_secret_value()}
        try:
            with httpx.Client(
                base_url=self._base_url,
                headers=headers,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                response = client.request(method, path, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise PlatformAdminError(f"admin-api {what}: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise PlatformAdminError(f"admin-api {what} answered {response.status_code}")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            raise PlatformAdminError(f"admin-api {what} answered a non-JSON body") from None
        if not isinstance(body, dict):
            raise PlatformAdminError(f"admin-api {what} answered an unexpected body")
        return body

    def ensure_user(self, email: str) -> int:
        """The platform user id for `email`, created if it does not exist."""
        body = self._call("create user", "POST", "/admin/users", json={"email": email})
        user_id = body.get("id")
        if not isinstance(user_id, int):
            raise PlatformAdminError("admin-api create user answered without an id")
        return user_id

    def mint_token(self, user_id: int, scopes: Sequence[str]) -> MintedToken:
        body = self._call(
            "mint token",
            "POST",
            f"/admin/users/{int(user_id)}/tokens",
            params={"scopes": ",".join(scopes)},
        )
        token_id, token = body.get("id"), body.get("token")
        if not isinstance(token_id, int) or not isinstance(token, str) or not token:
            raise PlatformAdminError("admin-api mint token answered without a token")
        if sorted(body.get("scopes") or []) != sorted(scopes):
            raise PlatformAdminError("admin-api mint token answered other scopes")
        return MintedToken(id=token_id, token=token)

    def revoke_token(self, token_id: int) -> None:
        self._call("revoke token", "DELETE", f"/admin/tokens/{int(token_id)}")

    def update_user(self, user_id: int, *, data: dict, max_concurrent_bots: int) -> None:
        """Merge `data` into the platform user's record and set its concurrent-bot cap."""
        self._call(
            "update user",
            "PATCH",
            f"/admin/users/{int(user_id)}",
            json={"data": data, "max_concurrent_bots": int(max_concurrent_bots)},
        )
