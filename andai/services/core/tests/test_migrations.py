"""Migrations run as the non-superuser owner role, up and down, and leave the expected grants."""

from __future__ import annotations

from alembic import command
from sqlalchemy import Engine, create_engine, text

from andai_core.db.models import (
    GLOBAL_TABLES,
    INTAKE_TABLES,
    ORG_SCOPED_TABLES,
    SYSTEM_TABLES,
    Base,
)
from conftest import Postgres, alembic_config

ALL_TABLES = (
    set(GLOBAL_TABLES) | set(ORG_SCOPED_TABLES) | set(SYSTEM_TABLES) | set(INTAKE_TABLES)
)


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
        ALL_TABLES
        - {"audit_events", "mom_documents", "meeting_removals"}
        - set(SYSTEM_TABLES)
        - set(INTAKE_TABLES)
    ):
        assert granted[table] == crud, table
    assert granted["audit_events"] == {"INSERT"}
    assert granted["mom_documents"] == {"SELECT", "INSERT"}  # a version is never changed
    assert granted["meeting_removals"] == {"SELECT", "INSERT"}  # a removal is a fact
    for table in set(SYSTEM_TABLES) - {"platform_webhook_events"}:  # kept, never deleted
        assert granted[table] == {"SELECT", "INSERT", "UPDATE"}, table
    assert granted["platform_webhook_events"] == {"SELECT", "INSERT"}  # an event is a fact
    # Column SELECT without `raw` only; andai-intake writes through the 0012 functions.
    assert granted["inbound_mail"] == set()
    assert tuple(app_role) == (False, False, False, False)
    assert version_table is False


# andai_scan's UPDATE column grants at head: the ingest columns (0006), the deletion lease (0009),
# the MoM and recap state and leases (0010), and the inbound mail state (0012).
HEAD_SCAN_UPDATES = 3 + 1 + 5 + 15

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
            "inbound_mail",
            "bot_settings",
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
            "inbound_mail",
            "bot_settings",
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
            "inbound_mail",
            "bot_settings",
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
        assert _tables(engine) == ALL_TABLES - {
            "meeting_removals",
            "platform_webhook_events",
            "inbound_mail",
            "bot_settings",
        }
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


# ─── 0012: the notetaker mailbox ──────────────────────────────────────────────────────────────────

INBOUND_FUNCTIONS = (
    "andai_store_inbound",
    "andai_claim_inbound",
    "andai_finish_inbound",
    "andai_release_inbound",
    "andai_purge_inbound",
)


def _inbound_state(engine: Engine) -> tuple[bool, int]:
    """(inbound_mail present, inbound functions)."""
    with engine.connect() as conn:
        functions = conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = ANY(:n)"),
            {"n": list(INBOUND_FUNCTIONS)},
        ).scalar_one()
    return "inbound_mail" in _tables(engine), functions


def test_inbound_mail_downgrade_to_0011_and_upgrade_again(migrated: Postgres) -> None:
    config = alembic_config(migrated.owner_url)
    engine = create_engine(migrated.superuser_url)
    try:
        assert _inbound_state(engine) == (True, 5)

        command.downgrade(config, "0011")
        assert _revision(engine) == "0011"
        assert _inbound_state(engine) == (False, 0)
        assert _scan_objects(engine)[2] == HEAD_SCAN_UPDATES - 15

        command.upgrade(config, "head")
        assert _inbound_state(engine) == (True, 5)
        assert _scan_objects(engine)[2] == HEAD_SCAN_UPDATES
    finally:
        engine.dispose()


def test_inbound_functions_belong_to_the_scan_role(migrated: Postgres) -> None:
    engine = create_engine(migrated.superuser_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
                    " has_function_privilege('andai_app', p.oid, 'EXECUTE'),"
                    " has_function_privilege('andai_owner', p.oid, 'EXECUTE')"
                    " FROM pg_proc p WHERE p.proname = ANY(:n)"
                ),
                {"n": list(INBOUND_FUNCTIONS)},
            ).all()
            forced = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relname = 'inbound_mail'"
                )
            ).one()
            policies = {
                row.policyname: (row.cmd, list(row.roles))
                for row in conn.execute(
                    text("SELECT policyname, cmd, roles FROM pg_policies"
                         " WHERE tablename = 'inbound_mail'")
                )
            }
    finally:
        engine.dispose()
    assert policies == {
        "org_read": ("SELECT", ["public"]),
        "scan_read": ("SELECT", ["andai_scan"]),
        "scan_insert": ("INSERT", ["andai_scan"]),
        "scan_update": ("UPDATE", ["andai_scan"]),
        "scan_purge": ("DELETE", ["andai_scan"]),
    }
    assert {row[0] for row in rows} == set(INBOUND_FUNCTIONS)
    for name, definer, owner, config, app_may, owner_may in rows:
        assert (definer, owner, app_may) == (True, "andai_scan", True), name
        assert config == ["search_path=public, pg_temp"], name
        assert owner_may is True, name  # a member of andai_scan, so it inherits EXECUTE
    assert tuple(forced) == (True, True)


def _store(engine: Engine, raw: bytes, **envelope):
    from andai_core.db.session import transaction

    with transaction(engine) as tx:
        return tx.execute(
            text(
                "SELECT id, duplicate FROM andai_store_inbound(:raw, :mail_from, :rcpt,"
                " CAST(:peer AS inet), :helo, :message_id)"
            ),
            {
                "raw": raw,
                "mail_from": envelope.get("mail_from", "organiser@example.test"),
                "rcpt": envelope.get("rcpt", ["notetaker@munshi.test"]),
                "peer": envelope.get("peer", "192.0.2.25"),
                "helo": envelope.get("helo", "mx.example.test"),
                "message_id": envelope.get("message_id", "<m@example.test>"),
            },
        ).one()


def _claim(engine: Engine, wanted, max_rows: int = 1000) -> dict:
    """Claim due messages and return the claimed rows among `wanted` ids, by id."""
    from andai_core.db.session import transaction

    with transaction(engine) as tx:
        rows = tx.execute(text("SELECT * FROM andai_claim_inbound(:m)"), {"m": max_rows}).all()
    return {row.id: row for row in rows if row.id in set(wanted)}


def _finish(engine: Engine, mail_id, **fields) -> bool:
    from andai_core.db.session import transaction

    values = {
        "status": "quarantined",
        "outcome": "quarantined",
        "reason": "unknown_user",
        "ignore": None,
        "org": None,
        "user": None,
        "sender": "member@a.example.test",
        "auth": "dmarc_pass",
        "summary": '{"spf": "pass", "dkim": "pass"}',
        "subject": "Weekly sync",
        **fields,
    }
    with transaction(engine) as tx:
        return tx.execute(
            text(
                "SELECT andai_finish_inbound(:id, :status, :outcome, :reason, :ignore, :org,"
                " :user, :sender, :auth, CAST(:summary AS jsonb), :subject)"
            ),
            {"id": mail_id, **values},
        ).scalar_one()


def _visible(engine: Engine, org_id) -> set:
    from andai_core.db.session import transaction

    with transaction(engine, org_id=org_id) as tx:
        return set(tx.execute(text("SELECT id FROM inbound_mail")).scalars())


def test_inbound_store_keeps_the_bytes_once_and_shows_them_to_no_org(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    import hashlib
    import secrets

    from conftest import bootstrap_org, new_user

    raw = b"Message-ID: <x@example.test>\r\nSubject: hi\r\n\r\nbody " + secrets.token_bytes(16)
    first = _store(app_engine, raw, rcpt=["notetaker@munshi.test", "Notes@munshi.test"])
    again = _store(app_engine, raw)
    assert first.duplicate is False
    assert (again.id, again.duplicate) == (first.id, True)

    with superuser_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, raw, raw_sha256, mail_from, rcpt_to, host(peer_ip) AS peer, helo,"
                " message_id, status, attempts, org_id FROM inbound_mail WHERE raw_sha256 = :s"
            ),
            {"s": hashlib.sha256(raw).hexdigest()},
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert bytes(row.raw) == raw
    assert (row.mail_from, row.rcpt_to, row.peer, row.helo, row.message_id) == (
        "organiser@example.test",
        ["notetaker@munshi.test", "Notes@munshi.test"],
        "192.0.2.25",
        "mx.example.test",
        "<m@example.test>",
    )
    assert (row.status, row.attempts, row.org_id) == ("received", 0, None)

    # A row nobody attributed is visible to no org, nor without an org.
    org = bootstrap_org(app_engine, new_user(app_engine))
    assert first.id not in _visible(app_engine, org)
    assert first.id not in _visible(app_engine, None)


def test_inbound_store_refuses_an_empty_message_or_no_recipient(app_engine: Engine) -> None:
    import pytest
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="empty message"):
        _store(app_engine, b"")
    with pytest.raises(DBAPIError, match="no recipient"):
        _store(app_engine, b"x", rcpt=[])


def test_inbound_finish_attributes_a_message_to_exactly_one_org(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    import json
    import secrets

    from conftest import bootstrap_org, new_user

    user_a = new_user(app_engine)
    org_a = bootstrap_org(app_engine, user_a)
    org_b = bootstrap_org(app_engine, new_user(app_engine))
    stored = _store(app_engine, secrets.token_bytes(64))
    other = _store(app_engine, secrets.token_bytes(64))

    claimed = _claim(app_engine, {stored.id, other.id})
    assert set(claimed) == {stored.id, other.id}
    assert claimed[stored.id].attempts == 1
    subject = "S" * 300
    assert _finish(app_engine, stored.id, org=org_a, user=user_a, subject=subject) is True
    assert _finish(app_engine, stored.id) is False  # no longer in processing

    with superuser_engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM inbound_mail WHERE id = :i"), {"i": stored.id}
        ).one()._mapping
    assert row["status"] == "quarantined"
    assert row["outcome"] == "quarantined"
    assert row["quarantine_reason"] == "unknown_user"
    assert row["ignore_reason"] is None
    assert (row["org_id"], row["resolved_user_id"]) == (org_a, user_a)
    assert (row["sender"], row["sender_auth"]) == ("member@a.example.test", "dmarc_pass")
    assert row["auth_summary"] == json.loads('{"spf": "pass", "dkim": "pass"}')
    assert row["subject_trim"] == "S" * 120
    assert row["processed_at"] is not None and row["lease_until"] is None
    assert row["raw"] is not None  # finishing keeps the bytes until the purge

    assert stored.id in _visible(app_engine, org_a)
    assert stored.id not in _visible(app_engine, org_b)
    assert other.id not in _visible(app_engine, org_a)  # still unattributed

    import pytest
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="status must be one of"):
        _finish(app_engine, other.id, status="received")
    with pytest.raises(DBAPIError, match="ck_inbound_mail_quarantined_has_reason"):
        _finish(app_engine, other.id, reason=None)
    assert _finish(
        app_engine, other.id, status="ignored", outcome="ignored", reason=None,
        ignore="auto_join_off", org=org_b,
    ) is True
    assert other.id in _visible(app_engine, org_b)


def test_inbound_release_and_an_expired_lease_make_a_message_claimable_again(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    import secrets

    from andai_core.db.session import transaction

    released = _store(app_engine, secrets.token_bytes(64))
    crashed = _store(app_engine, secrets.token_bytes(64))
    assert set(_claim(app_engine, {released.id, crashed.id})) == {released.id, crashed.id}
    assert _claim(app_engine, {released.id, crashed.id}) == {}  # leased

    with transaction(app_engine) as tx:
        assert tx.execute(
            text("SELECT andai_release_inbound(:i, :e)"), {"i": released.id, "e": "x" * 500}
        ).scalar_one() is True
    with superuser_engine.begin() as conn:  # the processor holding `crashed` died
        conn.execute(
            text("UPDATE inbound_mail SET lease_until = now() - interval '1 second' WHERE id = :i"),
            {"i": crashed.id},
        )
        last_error = conn.execute(
            text("SELECT status, last_error FROM inbound_mail WHERE id = :i"), {"i": released.id}
        ).one()
    assert (last_error.status, len(last_error.last_error)) == ("received", 200)

    again = _claim(app_engine, {released.id, crashed.id})
    assert {i: r.attempts for i, r in again.items()} == {released.id: 2, crashed.id: 2}


def test_app_role_cannot_write_inbound_mail_or_read_its_raw_bytes(
    app_engine: Engine,
) -> None:
    import pytest
    from sqlalchemy.exc import ProgrammingError

    from andai_core.db.session import transaction

    statements = (
        "UPDATE inbound_mail SET status = 'done'",
        "DELETE FROM inbound_mail",
        "INSERT INTO inbound_mail (raw_sha256, mail_from, rcpt_to)"
        " VALUES (repeat('a', 64), 'x', ARRAY['y'])",
        "SELECT raw FROM inbound_mail",
    )
    for statement in statements:
        with pytest.raises(ProgrammingError, match="permission denied"):
            with transaction(app_engine) as tx:
                tx.execute(text(statement))


# ─── 0013: sender lookup for the notetaker mailbox ────────────────────────────────────────────────

SENDER_FUNCTIONS = ("andai_resolve_sender", "andai_orgs_for_domain")
# What andai_scan may SELECT on the identity tables at head: 0005 (orgs id, is_canary) and 0013.
SENDER_SCAN_SELECT = {
    "users": {"id", "email", "status"},
    "org_memberships": {"org_id", "user_id"},
    "team_memberships": {"team_id", "org_id", "user_id"},
    "orgs": {"id", "is_canary", "allowed_email_domains"},
}


def _sender_lookup_state(engine: Engine) -> tuple[int, dict[str, set[str]], set[str]]:
    """(functions, andai_scan SELECT columns per identity table, tables with a scan_read policy)."""
    with engine.connect() as conn:
        functions = conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = ANY(:n)"),
            {"n": list(SENDER_FUNCTIONS)},
        ).scalar_one()
        columns: dict[str, set[str]] = {table: set() for table in SENDER_SCAN_SELECT}
        for table, column in conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.column_privileges"
                " WHERE grantee = 'andai_scan' AND privilege_type = 'SELECT'"
                " AND table_name = ANY(:t)"
            ),
            {"t": list(SENDER_SCAN_SELECT)},
        ):
            columns[table].add(column)
        policies = set(
            conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE policyname = 'scan_read'"
                    " AND tablename IN ('org_memberships', 'team_memberships')"
                )
            ).scalars()
        )
    return functions, columns, policies


def test_sender_lookup_downgrade_to_0012_and_upgrade_again(migrated: Postgres) -> None:
    config = alembic_config(migrated.owner_url)
    engine = create_engine(migrated.superuser_url)
    at_head = (2, SENDER_SCAN_SELECT, {"org_memberships", "team_memberships"})
    try:
        assert _sender_lookup_state(engine) == at_head

        command.downgrade(config, "0012")
        assert _revision(engine) == "0012"
        assert _sender_lookup_state(engine) == (
            0,
            {"users": set(), "org_memberships": set(), "team_memberships": set(),
             "orgs": {"id", "is_canary"}},
            set(),
        )
        assert _inbound_state(engine) == (True, 5)  # 0012 untouched

        command.upgrade(config, "head")
        assert _sender_lookup_state(engine) == at_head
    finally:
        engine.dispose()


def test_sender_lookup_functions_belong_to_the_scan_role(migrated: Postgres) -> None:
    engine = create_engine(migrated.superuser_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
                    " pg_get_function_result(p.oid),"
                    " has_function_privilege('public', p.oid, 'EXECUTE'),"
                    " has_function_privilege('andai_app', p.oid, 'EXECUTE')"
                    " FROM pg_proc p WHERE p.proname = ANY(:n)"
                ),
                {"n": list(SENDER_FUNCTIONS)},
            ).all()
    finally:
        engine.dispose()
    results = {row[0]: row[4] for row in rows}
    assert results == {
        "andai_resolve_sender": "TABLE(user_id uuid, org_id uuid, team_id uuid)",
        "andai_orgs_for_domain": "SETOF uuid",
    }
    for name, definer, owner, config, _, public_may, app_may in rows:
        assert (definer, owner, public_may, app_may) == (True, "andai_scan", False, True), name
        assert config == ["search_path=public, pg_temp"], name


def test_resolve_sender_returns_active_memberships_only(app_engine: Engine) -> None:
    import uuid

    from andai_core.db.models import Org, OrgMembership, Team, TeamMembership, User
    from andai_core.db.session import transaction

    domain = f"d{uuid.uuid4().hex[:12]}.example.test"
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    active, disabled = uuid.uuid4(), uuid.uuid4()
    team_1, team_2 = uuid.uuid4(), uuid.uuid4()
    with transaction(app_engine) as tx:
        tx.add(User(id=active, email=f"a@{domain}", display_name="A", status="active"))
        tx.add(User(id=disabled, email=f"d@{domain}", display_name="D", status="disabled"))
    for org in (org_a, org_b):
        with transaction(app_engine, org_id=org) as tx:
            tx.add(Org(id=org, name=f"O {org.hex[:6]}", slug=f"o-{org.hex}",
                       allowed_email_domains=[domain] if org == org_a else []))
            tx.flush()
            tx.add(OrgMembership(org_id=org, user_id=active, role="member"))
            tx.add(OrgMembership(org_id=org, user_id=disabled, role="member"))
    with transaction(app_engine, org_id=org_b) as tx:
        tx.add(Team(id=team_1, org_id=org_b, name="T1"))
        tx.add(Team(id=team_2, org_id=org_b, name="T2"))
        tx.flush()
        tx.add(TeamMembership(team_id=team_1, org_id=org_b, user_id=active, role="member"))

    with transaction(app_engine) as tx:
        rows = tx.execute(
            text("SELECT * FROM andai_resolve_sender(:e)"), {"e": f" A@{domain.upper()} "}
        ).all()
        none = tx.execute(text("SELECT * FROM andai_resolve_sender(:e)"), {"e": f"d@{domain}"}).all()
        orgs = list(
            tx.execute(text("SELECT andai_orgs_for_domain(:d)"), {"d": domain.upper()}).scalars()
        )
    assert {(r.user_id, r.org_id, r.team_id) for r in rows} == {
        (active, org_a, None), (active, org_b, team_1)
    }
    assert none == []
    assert orgs == [org_a]

    with transaction(app_engine, org_id=org_b) as tx:  # a second team: no team
        tx.add(TeamMembership(team_id=team_2, org_id=org_b, user_id=active, role="member"))
    with transaction(app_engine) as tx:
        rows = tx.execute(text("SELECT * FROM andai_resolve_sender(:e)"), {"e": f"a@{domain}"}).all()
    assert {(r.org_id, r.team_id) for r in rows} == {(org_a, None), (org_b, None)}


# ─── 0014: bot.autoJoin per org and team ──────────────────────────────────────────────────────────


def _bot_settings_state(engine: Engine) -> tuple[bool, tuple, set[str], bool]:
    """(table present, (RLS on, forced), policies, NULLS NOT DISTINCT on (org_id, team_id))."""
    with engine.connect() as conn:
        flags = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                " WHERE relname = 'bot_settings' AND relkind = 'r'"
            )
        ).one_or_none()
        policies = set(
            conn.execute(
                text("SELECT policyname FROM pg_policies WHERE tablename = 'bot_settings'")
            ).scalars()
        )
        nulls_not_distinct = conn.execute(
            text(
                "SELECT i.indnullsnotdistinct FROM pg_index i"
                " JOIN pg_class c ON c.oid = i.indexrelid"
                " WHERE c.relname = 'uq_bot_settings_org_id_team_id'"
            )
        ).scalar_one_or_none()
    return (
        "bot_settings" in _tables(engine),
        tuple(flags) if flags is not None else (),
        policies,
        bool(nulls_not_distinct),
    )


def test_bot_settings_downgrade_to_0013_and_upgrade_again(migrated: Postgres) -> None:
    config = alembic_config(migrated.owner_url)
    engine = create_engine(migrated.superuser_url)
    at_head = (True, (True, True), {"org_isolation"}, True)
    try:
        assert _bot_settings_state(engine) == at_head

        command.downgrade(config, "0013")
        assert _revision(engine) == "0013"
        assert _bot_settings_state(engine) == (False, (), set(), False)
        assert _sender_lookup_state(engine)[0] == 2  # 0013 untouched

        command.upgrade(config, "head")
        assert _revision(engine) == "0014"
        assert _bot_settings_state(engine) == at_head
    finally:
        engine.dispose()
