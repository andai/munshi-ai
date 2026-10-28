# andai/services/core/migrations

Alembic migrations for the `andai` database, run as the owner role `andai_owner`
(`make -C andai db-migrate`, or `python -m alembic upgrade head` next to `alembic.ini`).
`db-init.sql` creates the database and the `andai_owner` / `andai_app` / `andai_scan` roles; it is run by
`make -C andai db-init` and by the test fixtures, never by a migration.
