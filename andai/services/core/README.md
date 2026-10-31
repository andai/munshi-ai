# andai/services/core

The andai-core service (FastAPI, Python 3.12): `Dockerfile`, `pyproject.toml`, the `andai_core`
package, Alembic `migrations/` (with `alembic.ini`) and `tests/`. HTTP surface: `GET /healthz` and
the `/api/v1` auth routes (login, TOTP two-factor login and enrollment, logout, `me`, password
reset, invite acceptance, bootstrap), org routes (`/api/v1/org/security`, member 2FA reset,
members and invites under `/api/v1/org/users`, the share picker's `/api/v1/org/directory`, teams
under `/api/v1/teams`), meeting routes (the list `GET /api/v1/meetings`, `POST
/api/v1/meetings/join`, `GET /api/v1/meetings/{id}` with its stop, transcript, live, minutes and
share routes) and keyword search (`GET /api/v1/search`).
A background reconciler keeps meeting status and consent records in step with the meeting
platform (`ANDAI_RECONCILER=off` turns it off).

andai-core keeps identity and tenancy data in its own database `andai` on the stack's Postgres,
isolated per org by row-level security. `make -C andai db-init` creates the database and its roles;
`make -C andai db-migrate` migrates it (`make -C andai up` runs both).
