# andai/services/core/andai_core/platform

Each org's principal on the meeting platform:

- `admin_client.py` — `HttpPlatformAdmin`, the admin-api client (`PLATFORM_ADMIN_URL`,
  `PLATFORM_ADMIN_TOKEN`): find-or-create a user, mint a scoped token, revoke a token.
- `principals.py` — `provision` (the user `org-<org_id>@principals.munshi.invalid` and a `bot,tx`
  key), `store` (sealed under the KEK into `platform_principals`, one row per org) and
  `open_key`. The key is never returned by a route or logged.

`andai_core.org.service.create_org` provisions the principal before it inserts the org, so an org
never exists without one.
