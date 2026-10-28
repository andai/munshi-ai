"""Fixtures for andai-worker tests: andai-core's throwaway Postgres (initialised with db-init.sql
and migrated to head) and Mailpit, re-exported from `worker_support` so pytest registers them here.
Without Docker they skip, and `ANDAI_REQUIRE_DOCKER=1` turns the skip into a failure.

The directory is a package, so this module is `tests.conftest`, never the top-level `conftest`
that andai-core's test modules import from.
"""

from .worker_support import (  # noqa: F401  (pytest fixtures)
    app_engine,
    fresh_app_engine,
    mailpit,
    migrated,
    postgres,
    superuser_engine,
)
