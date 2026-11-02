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
    for table in (
        ALL_TABLES - {"audit_events", "mom_documents", "meeting_removals"} - set(SYSTEM_TABLES)
    ):
        assert granted[table] == crud, table
    assert granted["audit_events"] == {"INSERT"}
    assert granted["mom_documents"] == {"SELECT", "INSERT"}  # a version is never changed
    assert granted["meeting_removals"] == {"SELECT", "INSERT"}  # a removal is a fact
    for table in set(SYSTEM_TABLES) - {"platform_webhook_events"}:  # kept, never deleted
        assert granted[table] == {"SELECT", "INSERT", "UPDATE"}, table
    assert granted["platform_webhook_events"] == {"SELECT", "INSERT"}  # an event is a fact
    assert tuple(app_role) == (False, False, False, False)
    assert version_table is False


# andai_scan's UPDATE column grants at head: the ingest columns (0006), the deletion lease (0009),
# and the MoM and recap state and leases (0010).
HEAD_SCAN_UPDATES = 3 + 1 + 5

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
        assert _scan_objects(engine) == (1, 1, HEAD_SCAN_UPDATES)

        command.downgrade(config, "0005")
        assert _revision(engine) == "0005"
        assert _tables(engine) == ALL_TABLES - {
            "transcript_segments",
            "meeting_shares",
            "mom_documents",
            "meeting_removals",
            "platform_webhook_events",
        }
        assert not TRANSCRIPT_COLUMNS & _meeting_columns(engine)
        assert _scan_objects(engine) == (0, 0, 0)

        command.upgrade(config, "head")
        assert _tables(engine) == ALL_TABLES
        assert TRANSCRIPT_COLUMNS <= _meeting_columns(engine)
        assert _scan_objects(engine) == (1, 1, HEAD_SCAN_UPDATES)
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
        assert _tables(engine) == ALL_TABLES - {
            "meeting_shares",
            "mom_documents",
            "meeting_removals",
            "platform_webhook_events",
        }
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
        assert _tables(engine) == ALL_TABLES - {
            "mom_documents",
            "meeting_removals",
            "platform_webhook_events",
        }
        assert _search_state(engine) == (set(), False, False)

        command.upgrade(config, "head")
        assert _tables(engine) == ALL_TABLES
        assert _search_state(engine) == (SEARCH_INDEXES, True, True)
    finally:
        engine.dispose()


DELETION_COLUMNS = {
    "deleted_at",
    "deleted_by",
    "deletion_state",
    "deletion_attempts",
    "deletion_next_at",
}
DELETION_FUNCTIONS = ("andai_claim_deletions", "andai_deletion_backlog")


def _deletion_state(engine: Engine) -> tuple[bool, int, set[str], set[str], str]:
    """(deletion columns present, deletion functions, andai_scan SELECT and UPDATE grants on the
    deletion columns, the ingest claim's body with whitespace collapsed)."""
    with engine.connect() as conn:
        functions = conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = ANY(:n)"),
            {"n": list(DELETION_FUNCTIONS)},
        ).scalar_one()
        grants = conn.execute(
            text(
                "SELECT privilege_type, column_name FROM information_schema.column_privileges"
                " WHERE grantee = 'andai_scan' AND table_name = 'meetings'"
                " AND column_name = ANY(:c)"
            ),
            {"c": sorted(DELETION_COLUMNS)},
        ).all()
        body = conn.execute(
            text("SELECT prosrc FROM pg_proc WHERE proname = 'andai_claim_ingest'")
        ).scalar_one()
    selectable = {c for p, c in grants if p == "SELECT"}
    updatable = {c for p, c in grants if p == "UPDATE"}
    present = DELETION_COLUMNS <= _meeting_columns(engine)
    return present, functions, selectable, updatable, " ".join(body.split())


def test_deletion_downgrade_to_0008_and_upgrade_again(migrated: Postgres) -> None:
    config = alembic_config(migrated.owner_url)
    engine = create_engine(migrated.superuser_url)
    try:
        present, functions, selectable, updatable, head_body = _deletion_state(engine)
        assert (present, functions) == (True, 2)
        assert selectable == DELETION_COLUMNS - {"deleted_by"}
        assert updatable == {"deletion_next_at"}
        assert "deletion_state = 'none'" in head_body

        command.downgrade(config, "0008")
        assert _revision(engine) == "0008"
        assert _tables(engine) == ALL_TABLES - {"meeting_removals", "platform_webhook_events"}
        present, functions, selectable, updatable, body_0008 = _deletion_state(engine)
        assert (present, functions, selectable, updatable) == (False, 0, set(), set())
        assert _scan_objects(engine) == (1, 1, 3)

        # The downgrade restores the claim exactly as 0006 created it.
        command.downgrade(config, "0005")
        command.upgrade(config, "0008")
        assert _deletion_state(engine)[4] == body_0008
        assert "deletion" not in body_0008

        command.upgrade(config, "head")
        assert _tables(engine) == ALL_TABLES
        assert _deletion_state(engine)[:4] == (
            True,
            2,
            DELETION_COLUMNS - {"deleted_by"},
            {"deletion_next_at"},
        )
        assert _deletion_state(engine)[4] == head_body
    finally:
        engine.dispose()


# ─── 0010: the minutes pipeline ───────────────────────────────────────────────────────────────────

MOM_COLUMNS = {
    "mom_state",
    "mom_attempts",
    "mom_next_at",
    "mom_at",
    "mom_error",
    "mom_request",
    "mom_lease",
    "mom_lease_until",
    "recap_state",
    "recap_attempts",
    "recap_next_at",
    "recap_lease",
    "recap_at",
}
MOM_FUNCTIONS = (
    "andai_claim_mom",
    "andai_claim_recap",
    "andai_platform_completed",
    "andai_mom_stats",
)


def _mom_state(engine: Engine) -> tuple[bool, int, bool]:
    """(MoM columns present, MoM functions, webhook table present)."""
    with engine.connect() as conn:
        functions = conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = ANY(:n)"),
            {"n": list(MOM_FUNCTIONS)},
        ).scalar_one()
    return (
        MOM_COLUMNS <= _meeting_columns(engine),
        functions,
        "platform_webhook_events" in _tables(engine),
    )


def test_mom_pipeline_downgrade_to_0009_and_upgrade_again(migrated: Postgres) -> None:
    config = alembic_config(migrated.owner_url)
    engine = create_engine(migrated.superuser_url)
    try:
        assert _mom_state(engine) == (True, 4, True)

        command.downgrade(config, "0009")
        assert _revision(engine) == "0009"
        assert _mom_state(engine) == (False, 0, False)
        assert not MOM_COLUMNS & _meeting_columns(engine)
        assert _scan_objects(engine)[2] == 4

        command.upgrade(config, "head")
        assert _mom_state(engine) == (True, 4, True)
        assert _scan_objects(engine)[2] == HEAD_SCAN_UPDATES
    finally:
        engine.dispose()


def _fresh_database(postgres: Postgres) -> tuple[str, Engine]:
    import uuid

    name = f"andai_m{uuid.uuid4().hex[:12]}"
    admin = create_engine(postgres.url("postgres", postgres.superuser_password, "postgres"))
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CREATE DATABASE {name} OWNER andai_owner"))
        conn.execute(text(f"GRANT CONNECT ON DATABASE {name} TO andai_owner, andai_app"))
    admin.dispose()
    superuser = create_engine(postgres.url("postgres", postgres.superuser_password, name))
    with superuser.begin() as conn:
        conn.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO andai_app, andai_scan"))
    return name, superuser


def _drop_database(postgres: Postgres, name: str) -> None:
    admin = create_engine(postgres.url("postgres", postgres.superuser_password, "postgres"))
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    admin.dispose()


def test_meetings_over_before_0010_never_get_minutes_or_a_recap(migrated: Postgres) -> None:
    import json
    import uuid

    name, superuser = _fresh_database(migrated)
    try:
        owner_url = migrated.url("andai_owner", migrated.owner_password, name)
        command.upgrade(alembic_config(owner_url), "0009")
        org, user = uuid.uuid4(), uuid.uuid4()
        meetings = {
            "done_with_minutes": ("done", None),
            "done_without": ("done", None),
            "failed": ("failed", "internal_error"),
            "cancelled": ("cancelled", None),
            "recording": ("recording", None),
            "dispatching": ("dispatching", None),
        }
        ids = {label: uuid.uuid4() for label in meetings}
        with superuser.begin() as conn:  # the superuser is not bound by row-level security
            conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, status)"
                    " VALUES (:u, :e, 'U', 'active')"
                ),
                {"u": user, "e": f"{user.hex}@example.test"},
            )
            conn.execute(
                text("INSERT INTO orgs (id, name, slug) VALUES (:o, 'O', :s)"),
                {"o": org, "s": f"o-{org.hex}"},
            )
            for label, (status, reason) in meetings.items():
                conn.execute(
                    text(
                        "INSERT INTO meetings (id, org_id, requested_by, platform, join_url,"
                        " join_url_key, status, failure_reason)"
                        " VALUES (:m, :o, :u, 'zoom', :j, :k, :s, :r)"
                    ),
                    {
                        "m": ids[label],
                        "o": org,
                        "u": user,
                        "j": f"https://zoom.us/j/{label}",
                        "k": f"zoom.us/j/{ids[label].hex}",
                        "s": status,
                        "r": reason,
                    },
                )
            conn.execute(
                text(
                    "INSERT INTO mom_documents (meeting_id, version, org_id, template_id,"
                    " output_language, body, search_text, created_at)"
                    " VALUES (:m, 1, :o, 'standard', 'en', CAST(:b AS jsonb), 's',"
                    " '2026-10-30T10:00:00Z')"
                ),
                {"m": ids["done_with_minutes"], "o": org, "b": json.dumps({"summary": "s"})},
            )

        command.upgrade(alembic_config(owner_url), "head")

        with superuser.connect() as conn:
            rows = {
                row.id: (row.mom_state, row.recap_state, row.mom_at is not None)
                for row in conn.execute(
                    text("SELECT id, mom_state, recap_state, mom_at FROM meetings")
                )
            }
            forced = dict(
                conn.execute(
                    text(
                        "SELECT relname, relforcerowsecurity FROM pg_class"
                        " WHERE relname IN ('meetings', 'mom_documents')"
                    )
                ).all()
            )
        assert rows[ids["done_with_minutes"]] == ("stored", "skipped", True)
        for label in ("done_without", "failed", "cancelled"):
            assert rows[ids[label]] == ("skipped", "skipped", False), label
        for label in ("recording", "dispatching"):  # still running: minutes when it ends
            assert rows[ids[label]] == ("none", "none", False), label
        assert forced == {"meetings": True, "mom_documents": True}
    finally:
        superuser.dispose()
        _drop_database(migrated, name)


# ─── 0011: stored Devanagari forms re-normalised ──────────────────────────────────────────────────


def test_stored_devanagari_forms_are_refolded(migrated: Postgres) -> None:
    import json
    import uuid

    from andai_core.text import canonical, fold

    name, superuser = _fresh_database(migrated)
    try:
        owner_url = migrated.url("andai_owner", migrated.owner_password, name)
        command.upgrade(alembic_config(owner_url), "0010")
        org, user, meeting = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        body = {
            "templateId": "standard", "outputLanguage": "hi", "summary": "कुलकर्णी जी से नमस्ते",
            "decisions": [], "actionItems": [], "topics": [], "risks": [],
            "llmProfileId": str(uuid.uuid4()), "model": "m",
            "generatedAt": "2026-11-02T10:00:00Z",
        }  # fmt: skip
        with superuser.begin() as conn:  # the superuser is not bound by row-level security
            conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, status)"
                    " VALUES (:u, :e, 'U', 'active')"
                ),
                {"u": user, "e": f"{user.hex}@example.test"},
            )
            conn.execute(
                text("INSERT INTO orgs (id, name, slug) VALUES (:o, 'O', :s)"),
                {"o": org, "s": f"o-{org.hex}"},
            )
            conn.execute(
                text(
                    "INSERT INTO meetings (id, org_id, requested_by, platform, join_url,"
                    " join_url_key, status, title, title_search)"
                    " VALUES (:m, :o, :u, 'zoom', 'https://zoom.us/j/1', :k, 'done',"
                    " 'सुरक्षा समीक्षा', 'surksha samiksha')"
                ),
                {"m": meeting, "o": org, "u": user, "k": f"zoom.us/j/{meeting.hex}"},
            )
            for seq, (seg, raw, script, roman) in enumerate(
                [
                    ("seg_a", "कुलकर्णी जी report bhejenge", "mixed", "kulakrni ji report bhejenge"),
                    ("seg_b", "plain English only", "latn", "plain English only"),
                ]
            ):
                conn.execute(
                    text(
                        "INSERT INTO transcript_segments (meeting_id, id, org_id, seq, speaker,"
                        " text_raw, text_canonical, script, search_text)"
                        " VALUES (:m, :i, :o, :q, 'A', :r, :c, :s, :f)"
                    ),
                    {"m": meeting, "i": seg, "o": org, "q": seq, "r": raw, "c": roman,
                     "s": script, "f": roman.lower()},
                )  # fmt: skip
            conn.execute(
                text(
                    "INSERT INTO mom_documents (meeting_id, version, org_id, template_id,"
                    " output_language, body, search_text)"
                    " VALUES (:m, 1, :o, 'standard', 'hi', CAST(:b AS jsonb), 'kulakrni ji se namste')"
                ),
                {"m": meeting, "o": org, "b": json.dumps(body, ensure_ascii=False)},
            )

        command.upgrade(alembic_config(owner_url), "0011")

        with superuser.connect() as conn:
            segments = dict(
                conn.execute(
                    text("SELECT id, text_canonical || '|' || search_text FROM transcript_segments")
                ).all()
            )
            document = conn.execute(text("SELECT search_text FROM mom_documents")).scalar_one()
            title = conn.execute(text("SELECT title_search FROM meetings")).scalar_one()
            forced = dict(
                conn.execute(
                    text(
                        "SELECT relname, relforcerowsecurity FROM pg_class"
                        " WHERE relname IN ('transcript_segments', 'mom_documents', 'meetings')"
                    )
                ).all()
            )
        roman = canonical("कुलकर्णी जी report bhejenge")
        assert roman == "kulkarni ji report bhejenge"
        assert segments["seg_a"] == f"{roman}|{fold(roman)}"
        assert segments["seg_b"] == "plain English only|plain english only"  # not Devanagari
        assert document == "kulkarni ji se namaste"
        assert title == fold("सुरक्षा समीक्षा") == "suraksha samiksha"
        assert forced == {"transcript_segments": True, "mom_documents": True, "meetings": True}
    finally:
        superuser.dispose()
        _drop_database(migrated, name)
