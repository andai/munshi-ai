# andai/services/core

The andai-core service (FastAPI, Python 3.12): `Dockerfile`, `pyproject.toml`, the `andai_core`
package, Alembic `migrations/` (with `alembic.ini`) and `tests/`. HTTP surface: `GET /healthz` and
the `/api/v1` auth routes (login, logout, `me`, password reset, invite acceptance, bootstrap).

andai-core keeps identity and tenancy data in its own database `andai` on the stack's Postgres,
isolated per org by row-level security. `make -C andai db-init` creates the database and its roles;
`make -C andai db-migrate` migrates it (`make -C andai up` runs both).
