# andai/services/core/andai_core/platform

Each org's principal on the meeting platform:

- `admin_client.py` — `HttpPlatformAdmin`, the admin-api client (`PLATFORM_ADMIN_URL`,
  `PLATFORM_ADMIN_TOKEN`): find-or-create a user, mint a scoped token, revoke a token.
- `principals.py` — `provision` (the user `org-<org_id>@principals.munshi.invalid` and a `bot,tx`
  key), `configure` (the user's platform settings: storage prefix `org/<org_id>`, diagnostic tapes
  off, `ANDAI_ORG_MAX_CONCURRENT_BOTS`), `store` (sealed under the KEK into `platform_principals`,
  one row per org), `open_key`, and `sync` (re-applies `configure` to every existing principal;
  `python -m andai_core principals-sync`). The key is never returned by a route or logged.

`andai_core.org.service.create_org` provisions and configures the principal before it inserts the
org, so an org never exists without a configured one.
