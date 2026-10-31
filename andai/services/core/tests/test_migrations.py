"""Migrations run as the non-superuser owner role, up and down, and leave the expected grants."""

from __future__ import annotations

from alembic import command
from sqlalchemy import Engine, create_engine, text

from andai_core.db.models import GLOBAL_TABLES, ORG_SCOPED_TABLES, SYSTEM_TABLES, Base
from conftest import Postgres, alembic_config

ALL_TABLES = set(GLOBAL_TABLES) | set(ORG_SCOPED_TABLES) | set(SYSTEM_TABLES)


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
        return set(rows) - {"alembic_version"}


def _revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT to_regclass('public.alembic_version')")).scalar()
        if exists is None:
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_owner_role_is_not_privileged(postgres: Postgres) -> None:
    engine = create_engine(postgres.owner_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb"
                    " FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
    finally:
        engine.dispose()
    assert tuple(row) == ("andai_owner", False, False, False, False)


def test_models_cover_every_table() -> None:
    assert set(Base.metadata.tables) == ALL_TABLES


def test_upgrade_downgrade_upgrade(migrated: Postgres) -> None:
    config = alembic_config(migrated.owner_url)
    engine = create_engine(migrated.superuser_url)
    try:
        assert _tables(engine) == ALL_TABLES
        head = _revision(engine)
        assert head is not None

        command.downgrade(config, "base")
        assert _tables(engine) == set()
        assert _revision(engine) is None

        command.upgrade(config, "head")
        assert _tables(engine) == ALL_TABLES
        assert _revision(engine) == head
    finally:
        engine.dispose()


def test_tables_owned_by_owner_role(migrated: Postgres) -> None:
    engine = create_engine(migrated.superuser_url)
    try:
        with engine.connect() as conn:
            owners = dict(
                conn.execute(
                    text("SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public'")
                ).all()
            )
    finally:
        engine.dispose()
    assert set(owners.values()) == {"andai_owner"}


def test_app_role_grants(migrated: Postgres) -> None:
    engine = create_engine(migrated.superuser_url)
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    try:
        with engine.connect() as conn:
            granted = {
                table: {
                    p
                    for p in privileges
                    if conn.execute(
                        text("SELECT has_table_privilege('andai_app', :t, :p)"),
                        {"t": f"public.{table}", "p": p},
                    ).scalar_one()
                }
                for table in ALL_TABLES
            }
            app_role = conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb"
                    " FROM pg_roles WHERE rolname = 'andai_app'"
                )
            ).one()
            version_table = conn.execute(
                text("SELECT has_table_privilege('andai_app', 'public.alembic_version', 'SELECT')")
            ).scalar_one()
    finally:
        engine.dispose()

    crud = {"SELECT", "INSERT", "UPDATE", "DELETE"}
    for table in ALL_TABLES - {"audit_events", "mom_documents"} - set(SYSTEM_TABLES):
        assert granted[table] == crud, table
    assert granted["audit_events"] == {"INSERT"}
    assert granted["mom_documents"] == {"SELECT", "INSERT"}  # a version is never changed
    for table in SYSTEM_TABLES:  # canary runs and alert incidents are kept, never deleted
        assert granted[table] == {"SELECT", "INSERT", "UPDATE"}, table
    assert tuple(app_role) == (False, False, False, False)
    assert version_table is False


TRANSCRIPT_COLUMNS = {
    "title",
    "attendees",
    "transcript_state",
    "transcript_attempts",
    "transcript_next_at",
    "transcript_at",
}


def _meeting_columns(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'meetings'"
                )
            ).scalars()
        )


def _scan_objects(engine: Engine) -> tuple[int, int, int]:
    """(claim functions, scan_claim policies, andai_scan UPDATE column grants)."""
    with engine.connect() as conn:
        return (
            conn.execute(
                text("SELECT count(*) FROM pg_proc WHERE proname = 'andai_claim_ingest'")
            ).scalar_one(),
            conn.execute(
                text("SELECT count(*) FROM pg_policies WHERE policyname = 'scan_claim'")
            ).scalar_one(),
            conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.column_privileges"
                    " WHERE grantee = 'andai_scan' AND privilege_type = 'UPDATE'"
                )
            ).scalar_one(),
        )


def test_transcripts_downgrade_to_0005_and_upgrade_again(migrated: Postgres) -> None:
    config = alembic_config(migrated.owner_url)
    engine = create_engine(migrated.superuser_url)
    try:
        assert TRANSCRIPT_COLUMNS <= _meeting_columns(engine)
        assert _scan_objects(engine) == (1, 1, 3)

        command.downgrade(config, "0005")
        assert _revision(engine) == "0005"
        assert _tables(engine) == ALL_TABLES - {
            "transcript_segments", "meeting_shares", "mom_documents"
        }
        assert not TRANSCRIPT_COLUMNS & _meeting_columns(engine)
        assert _scan_objects(engine) == (0, 0, 0)

        command.upgrade(config, "head")
        assert _tables(engine) == ALL_TABLES
        assert TRANSCRIPT_COLUMNS <= _meeting_columns(engine)
        assert _scan_objects(engine) == (1, 1, 3)
    finally:
        engine.dispose()


LIST_INDEXES = {"ix_meetings_org_id_status_sort_time", "ix_meetings_org_id_requested_by"}


def _meeting_indexes(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'meetings'")
            ).scalars()
        )


def test_shares_downgrade_to_0006_and_upgrade_again(migrated: Postgres) -> None:
    config = alembic_config(migrated.owner_url)
    engine = create_engine(migrated.superuser_url)
    try:
        assert LIST_INDEXES <= _meeting_indexes(engine)

        command.downgrade(config, "0006")
        assert _revision(engine) == "0006"
        assert _tables(engine) == ALL_TABLES - {"meeting_shares", "mom_documents"}
        assert not LIST_INDEXES & _meeting_indexes(engine)

        command.upgrade(config, "head")
        assert _tables(engine) == ALL_TABLES
        assert LIST_INDEXES <= _meeting_indexes(engine)
    finally:
        engine.dispose()


SEARCH_INDEXES = {
    "ix_transcript_segments_search_tsv",
    "ix_mom_documents_search_tsv",
    "ix_mom_documents_search_text_trgm",
    "ix_meetings_title_search_fts",
    "ix_meetings_title_search_trgm",
}


def _search_state(engine: Engine) -> tuple[set[str], bool, bool]:
    """(search indexes present, pg_trgm installed, meetings.title_search and
    transcript_segments.search_tsv present)."""
    with engine.connect() as conn:
        indexes = set(
            conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            ).scalars()
        )
        trgm = conn.execute(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'")
        ).scalar_one()
        tsv = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'transcript_segments' AND column_name = 'search_tsv'"
            )
        ).scalar_one()
    columns = "title_search" in _meeting_columns(engine) and tsv == 1
    return indexes & SEARCH_INDEXES, trgm == 1, columns


def test_mom_search_downgrade_to_0007_and_upgrade_again(migrated: Postgres) -> None:
    config = alembic_config(migrated.owner_url)
    engine = create_engine(migrated.superuser_url)
    try:
        assert _search_state(engine) == (SEARCH_INDEXES, True, True)

        command.downgrade(config, "0007")
        assert _revision(engine) == "0007"
        assert _tables(engine) == ALL_TABLES - {"mom_documents"}
        assert _search_state(engine) == (set(), False, False)

        command.upgrade(config, "head")
        assert _tables(engine) == ALL_TABLES
        assert _search_state(engine) == (SEARCH_INDEXES, True, True)
    finally:
        engine.dispose()
