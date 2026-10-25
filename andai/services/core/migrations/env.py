"""Alembic environment for the andai database.

Runs only as the owner role `andai_owner` (created by `make -C andai db-init`), so every table is
owned by it and the app role gets exactly the grants the migrations give it.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from andai_core.db.models import Base
from andai_core.settings import get_settings

OWNER_ROLE = "andai_owner"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    secret = get_settings().db_url_owner
    if secret is None:
        raise RuntimeError("ANDAI_DB_URL_OWNER is not set; migrations run as the owner role")
    return secret.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        role = connection.execute(text("SELECT current_user")).scalar_one()
        if role != OWNER_ROLE:
            raise RuntimeError(f"migrations must run as {OWNER_ROLE}, not {role}")
        connection.commit()  # end the role check's implicit transaction; Alembic opens its own
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
