"""Postgres row-level security isolates orgs for the app role.

Every test runs as `andai_app` through `andai_core.db.session.transaction()` (or a raw connection
for the raw-query cases). The superuser engine is used only to check what is really stored.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, insert, select, text
from sqlalchemy.exc import ProgrammingError

from andai_core.db.models import (
    GLOBAL_TABLES,
    ORG_SCOPED_TABLES,
    SYSTEM_TABLES,
    AuditEvent,
    ConsentRecord,
    Invite,
    Meeting,
    MeetingShare,
    MomDocument,
    Org,
    OrgMembership,
    PlatformPrincipal,
    Team,
    TeamMembership,
    TranscriptSegment,
)
from andai_core.db.session import transaction
from conftest import Postgres, bootstrap_org, new_user

RLS_ERROR = "row-level security"

# Org-scoped tables the app role can read, with the column that carries the org.
READABLE = {
    "orgs": "id",
    "org_memberships": "org_id",
    "teams": "org_id",
    "team_memberships": "org_id",
    "invites": "org_id",
    "platform_principals": "org_id",
    "meetings": "org_id",
    "consent_records": "org_id",
    "transcript_segments": "org_id",
    "meeting_shares": "org_id",
    "mom_documents": "org_id",
}


def _token_hash() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


@dataclass(frozen=True)
class SeededOrg:
    org_id: uuid.UUID
    owner_id: uuid.UUID
    team_id: uuid.UUID
    invite_id: uuid.UUID
    invite_token_hash: str
    meeting_id: uuid.UUID


def _seed(engine: Engine) -> SeededOrg:
    owner_id = new_user(engine)
    org_id = bootstrap_org(engine, owner_id)
    team_id, invite_id, token_hash = uuid.uuid4(), uuid.uuid4(), _token_hash()
    with transaction(engine, org_id=org_id, user_id=owner_id) as tx:
        tx.add(Team(id=team_id, org_id=org_id, name="Engineering"))
        tx.flush()
        tx.add(TeamMembership(team_id=team_id, org_id=org_id, user_id=owner_id, role="lead"))
        tx.add(
            Invite(
                id=invite_id,
                org_id=org_id,
                email=f"invitee-{invite_id.hex}@example.test",
                role="member",
                token_hash=token_hash,
                invited_by=owner_id,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        tx.add(
            PlatformPrincipal(
                org_id=org_id, platform_user_id=1, key_ciphertext="sealed", key_id="test"
            )
        )
        tx.execute(
            insert(AuditEvent.__table__).values(
                id=uuid.uuid4(), org_id=org_id, actor_user_id=owner_id, action="org.create",
                target=f"org:{org_id}",
            )
        )
    meeting_id = _seed_meeting(engine, org_id, owner_id, status="recording", team_id=team_id)
    return SeededOrg(org_id, owner_id, team_id, invite_id, token_hash, meeting_id)


def _seed_meeting(
    engine: Engine,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    status: str,
    team_id: uuid.UUID | None = None,
) -> uuid.UUID:
    meeting_id = uuid.uuid4()
    with transaction(engine, org_id=org_id, user_id=owner_id) as tx:
        tx.add(
            Meeting(
                id=meeting_id,
                org_id=org_id,
                team_id=team_id,
                requested_by=owner_id,
                platform="zoom",
                native_meeting_id=f"{meeting_id.int % 10**11:011d}",
                join_url=f"https://zoom.us/j/{meeting_id.hex}?pwd=secret",
                join_url_key=f"zoom.us/j/{meeting_id.hex}",
                platform_meeting_id=meeting_id.int % 10**9,
                status=status,
                failure_reason="internal_error" if status == "failed" else None,
                settings_snapshot={"bot_name": "munshi.ai (recording)"},
            )
        )
        tx.flush()
        tx.add(
            ConsentRecord(
                meeting_id=meeting_id,
                org_id=org_id,
                bot_display_name="munshi.ai (recording)",
                disclosure_text="recording",
                privacy_notice_url="https://example.test/privacy",
                disclosure_channel="chat",
                admitted_at=datetime.now(UTC),
            )
        )
        tx.add(
            TranscriptSegment(
                meeting_id=meeting_id,
                id=f"seg_{meeting_id.hex[:24]}",
                org_id=org_id,
                seq=0,
                start_s=0.0,
                end_s=1.5,
                speaker="Asha",
                text_raw="कल की मीटिंग",
                text_canonical="kal ki meeting",
                script="deva",
                language="hi",
                search_text="kal ki miting",
            )
        )
        tx.add(
            MeetingShare(
                org_id=org_id,
                meeting_id=meeting_id,
                grantee_type="user",
                grantee_id=owner_id,
                granted_by=owner_id,
            )
        )
        tx.flush()
        tx.add(
            MomDocument(
                meeting_id=meeting_id,
                version=1,
                org_id=org_id,
                template_id="standard",
                output_language="hinglish",
                body={"meetingId": str(meeting_id), "version": 1, "summary": "kal ki meeting"},
                search_text="kal ki miting",
            )
        )
    return meeting_id


def _count(tx, table: str, column: str, org_id: uuid.UUID) -> int:
    return tx.execute(
        text(f"SELECT count(*) FROM {table} WHERE {column} = :org"), {"org": org_id}
    ).scalar_one()


@pytest.fixture
def org_a(app_engine: Engine) -> SeededOrg:
    return _seed(app_engine)


@pytest.fixture
def org_b(app_engine: Engine) -> SeededOrg:
    return _seed(app_engine)


def test_app_engine_connects_as_app_role_not_owner(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        user, superuser, bypass = conn.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles"
                " WHERE rolname = current_user"
            )
        ).one()
        owners = set(
            conn.execute(
                text("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
    assert (user, superuser, bypass) == ("andai_app", False, False)
    assert owners == {"andai_owner"}


def test_rls_enabled_and_forced_exactly_on_org_scoped_tables(superuser_engine: Engine) -> None:
    with superuser_engine.connect() as conn:
        flags = {
            name: (enabled, forced)
            for name, enabled, forced in conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
                )
            ).all()
        }
    for table in ORG_SCOPED_TABLES:
        assert flags[table] == (True, True), table
    for table in (*GLOBAL_TABLES, *SYSTEM_TABLES):
        assert flags[table] == (False, False), table


@pytest.mark.parametrize("table", sorted(READABLE))
def test_other_org_rows_are_invisible(
    app_engine: Engine, org_a: SeededOrg, org_b: SeededOrg, table: str
) -> None:
    column = READABLE[table]
    with transaction(app_engine, org_id=org_b.org_id) as tx:
        assert _count(tx, table, column, org_a.org_id) == 0
        assert _count(tx, table, column, org_b.org_id) >= 1
    with transaction(app_engine, org_id=org_a.org_id) as tx:
        assert _count(tx, table, column, org_a.org_id) >= 1


@pytest.mark.parametrize(
    ("table", "assignment"),
    [
        ("orgs", "name = 'hijacked'"),
        ("org_memberships", "role = 'member'"),
        ("teams", "name = 'hijacked'"),
        ("team_memberships", "role = 'member'"),
        ("invites", "role = 'admin'"),
        ("platform_principals", "key_id = 'hijacked'"),
        ("meetings", "status = 'cancelled'"),
        ("consent_records", "disclosure_detail = 'hijacked'"),
        ("transcript_segments", "speaker = 'hijacked'"),
        ("meeting_shares", "grantee_type = 'team'"),
    ],
)
def test_other_org_rows_cannot_be_updated_or_deleted(
    app_engine: Engine,
    superuser_engine: Engine,
    org_a: SeededOrg,
    org_b: SeededOrg,
    table: str,
    assignment: str,
) -> None:
    column = READABLE[table]
    with superuser_engine.connect() as conn:
        before = conn.execute(
            text(f"SELECT * FROM {table} WHERE {column} = :org ORDER BY 1"), {"org": org_a.org_id}
        ).all()
    with transaction(app_engine, org_id=org_b.org_id) as tx:
        updated = tx.execute(
            text(f"UPDATE {table} SET {assignment} WHERE {column} = :org"), {"org": org_a.org_id}
        )
        assert updated.rowcount == 0
    with transaction(app_engine, org_id=org_b.org_id) as tx:
        deleted = tx.execute(
            text(f"DELETE FROM {table} WHERE {column} = :org"), {"org": org_a.org_id}
        )
        assert deleted.rowcount == 0
    with superuser_engine.connect() as conn:
        after = conn.execute(
            text(f"SELECT * FROM {table} WHERE {column} = :org ORDER BY 1"), {"org": org_a.org_id}
        ).all()
    assert after == before and before


def test_rows_cannot_be_written_into_another_org(
    app_engine: Engine, org_a: SeededOrg, org_b: SeededOrg
) -> None:
    with pytest.raises(ProgrammingError, match=RLS_ERROR):
        with transaction(app_engine, org_id=org_b.org_id) as tx:
            tx.add(Team(org_id=org_a.org_id, name="Planted"))
    with pytest.raises(ProgrammingError, match=RLS_ERROR):
        with transaction(app_engine, org_id=org_b.org_id) as tx:
            tx.execute(
                insert(AuditEvent.__table__).values(
                    id=uuid.uuid4(), org_id=org_a.org_id, action="planted", target="x"
                )
            )
    with pytest.raises(ProgrammingError, match=RLS_ERROR):
        with transaction(app_engine, org_id=org_b.org_id) as tx:
            tx.execute(
                text("UPDATE invites SET org_id = :a WHERE org_id = :b"),
                {"a": org_a.org_id, "b": org_b.org_id},
            )


def test_minutes_cannot_be_written_into_another_org(
    app_engine: Engine, org_a: SeededOrg, org_b: SeededOrg
) -> None:
    with pytest.raises(ProgrammingError, match=RLS_ERROR):
        with transaction(app_engine, org_id=org_b.org_id) as tx:
            tx.add(
                MomDocument(
                    meeting_id=org_a.meeting_id,
                    version=2,
                    org_id=org_a.org_id,
                    template_id="standard",
                    output_language="en",
                    body={},
                    search_text="planted",
                )
            )


def test_the_app_role_may_not_update_or_delete_minutes(
    app_engine: Engine, org_a: SeededOrg
) -> None:
    """A minutes version is never changed, in any org: no UPDATE or DELETE grant at all."""
    for statement in ("UPDATE mom_documents SET search_text = 'x'", "DELETE FROM mom_documents"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            with transaction(app_engine, org_id=org_a.org_id) as tx:
                tx.execute(text(statement))


@pytest.mark.parametrize("table", sorted(READABLE))
def test_no_context_sees_no_rows(
    app_engine: Engine, superuser_engine: Engine, org_a: SeededOrg, table: str
) -> None:
    with superuser_engine.connect() as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() >= 1
    with transaction(app_engine) as tx:
        assert tx.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_raw_select_with_wrong_org_returns_zero_rows(
    migrated: Postgres, superuser_engine: Engine, org_a: SeededOrg, org_b: SeededOrg
) -> None:
    with superuser_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM teams")).scalar_one() >= 2

    engine = create_engine(migrated.app_url)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f"SET LOCAL app.org_id = '{uuid.uuid4()}'")
            assert conn.exec_driver_sql("SELECT * FROM teams").all() == []
            conn.rollback()

            conn.exec_driver_sql(f"SET LOCAL app.org_id = '{org_b.org_id}'")
            rows = conn.exec_driver_sql("SELECT id, org_id FROM teams").all()
            conn.rollback()
    finally:
        engine.dispose()
    assert rows and {org for _, org in rows} == {org_b.org_id}
    assert org_a.team_id not in {team for team, _ in rows}


def test_set_local_does_not_leak_to_next_transaction(
    migrated: Postgres, org_a: SeededOrg
) -> None:
    engine = create_engine(migrated.app_url, pool_size=1, max_overflow=0)
    try:
        with transaction(engine, org_id=org_a.org_id) as tx:
            first_pid = tx.execute(text("SELECT pg_backend_pid()")).scalar_one()
            assert _count(tx, "teams", "org_id", org_a.org_id) == 1
        with transaction(engine) as tx:
            second_pid = tx.execute(text("SELECT pg_backend_pid()")).scalar_one()
            leaked = tx.execute(text("SELECT current_setting('app.org_id', true)")).scalar()
            visible = tx.execute(text("SELECT count(*) FROM teams")).scalar_one()
    finally:
        engine.dispose()
    assert second_pid == first_pid  # same pooled connection
    assert leaked in (None, "")
    assert visible == 0


def test_owner_role_is_bound_by_forced_rls(owner_engine: Engine, org_a: SeededOrg) -> None:
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM teams")).scalar_one() == 0


def test_login_finds_memberships_with_only_user_id(
    app_engine: Engine, org_a: SeededOrg, org_b: SeededOrg
) -> None:
    # The owner of org A also joins org B (written under B's context); org A gets another member.
    with transaction(app_engine, org_id=org_b.org_id) as tx:
        tx.add(OrgMembership(org_id=org_b.org_id, user_id=org_a.owner_id, role="member"))
    other_member = new_user(app_engine)
    with transaction(app_engine, org_id=org_a.org_id) as tx:
        tx.add(OrgMembership(org_id=org_a.org_id, user_id=other_member, role="member"))

    with transaction(app_engine, user_id=org_a.owner_id) as tx:
        memberships = set(
            tx.execute(select(OrgMembership.org_id, OrgMembership.user_id, OrgMembership.role)).all()
        )
        teams_visible = tx.execute(text("SELECT count(*) FROM teams")).scalar_one()
    assert memberships == {
        (org_a.org_id, org_a.owner_id, "owner"),
        (org_b.org_id, org_a.owner_id, "member"),
    }
    assert teams_visible == 0  # a user context opens memberships only, not org data

    # The user context reads memberships but cannot write them.
    with pytest.raises(ProgrammingError, match=RLS_ERROR):
        with transaction(app_engine, user_id=other_member) as tx:
            tx.add(OrgMembership(org_id=org_b.org_id, user_id=other_member, role="owner"))


def test_invite_lookup_by_token_hash_only(
    app_engine: Engine, org_a: SeededOrg, org_b: SeededOrg
) -> None:
    second_hash = _token_hash()
    with transaction(app_engine, org_id=org_a.org_id) as tx:
        tx.add(
            Invite(
                org_id=org_a.org_id,
                email="second@example.test",
                role="member",
                token_hash=second_hash,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )

    with transaction(app_engine, invite_token_hash=org_a.invite_token_hash) as tx:
        found = tx.execute(text("SELECT id, org_id FROM invites")).all()
        updated = tx.execute(
            text("UPDATE invites SET role = 'owner' WHERE token_hash = :h"),
            {"h": org_a.invite_token_hash},
        ).rowcount
    assert found == [(org_a.invite_id, org_a.org_id)]
    assert updated == 0  # the token policy is read-only; accepting runs under the org context

    with transaction(app_engine) as tx:
        assert tx.execute(text("SELECT * FROM invites")).all() == []
    with transaction(app_engine, invite_token_hash=_token_hash()) as tx:
        assert tx.execute(text("SELECT * FROM invites")).all() == []


def test_bootstrap_inserts_org_and_owner_after_setting_org_id(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    owner_id = new_user(app_engine)
    org_id = bootstrap_org(app_engine, owner_id)
    with superuser_engine.connect() as conn:
        orgs = conn.execute(text("SELECT count(*) FROM orgs WHERE id = :o"), {"o": org_id})
        assert orgs.scalar_one() == 1
        assert conn.execute(
            text("SELECT role FROM org_memberships WHERE org_id = :o AND user_id = :u"),
            {"o": org_id, "u": owner_id},
        ).scalar_one() == "owner"

    # Without the context, or under a different org id, the same inserts are refused.
    with pytest.raises(ProgrammingError, match=RLS_ERROR):
        with transaction(app_engine) as tx:
            tx.add(Org(id=uuid.uuid4(), name="No context", slug=f"x-{uuid.uuid4().hex}"))
    with pytest.raises(ProgrammingError, match=RLS_ERROR):
        with transaction(app_engine, org_id=uuid.uuid4()) as tx:
            tx.add(Org(id=uuid.uuid4(), name="Wrong context", slug=f"y-{uuid.uuid4().hex}"))


# ─── cross-org listing of principal org ids (migration 0003) ──────────────────────────────────────


def test_principal_org_ids_lists_every_org_while_the_table_stays_hidden(
    app_engine: Engine, org_a: SeededOrg, org_b: SeededOrg
) -> None:
    with transaction(app_engine) as tx:  # andai_app, no app.org_id
        listed = set(tx.scalars(text("SELECT andai_principal_org_ids()")))
        direct = tx.execute(text("SELECT count(*) FROM platform_principals")).scalar_one()
    assert {org_a.org_id, org_b.org_id} <= listed
    assert direct == 0


def test_principal_org_ids_reveals_org_ids_only(superuser_engine: Engine) -> None:
    with superuser_engine.connect() as conn:
        fn = conn.execute(
            text(
                "SELECT p.prosecdef, r.rolname, p.proconfig, pg_get_function_result(p.oid),"
                " has_function_privilege('public', p.oid, 'EXECUTE'),"
                " has_function_privilege('andai_app', p.oid, 'EXECUTE')"
                " FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner"
                " WHERE p.proname = 'andai_principal_org_ids'"
            )
        ).one()
        columns = {
            column: conn.execute(
                text("SELECT has_column_privilege('andai_scan', 'platform_principals', :c,"
                     " 'SELECT')"),
                {"c": column},
            ).scalar_one()
            for column in ("org_id", "platform_user_id", "key_ciphertext", "key_id")
        }
        other_tables = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables t WHERE t.schemaname = 'public' AND ("
                " has_table_privilege('andai_scan', format('%I.%I', t.schemaname, t.tablename),"
                "   'SELECT, INSERT, UPDATE, DELETE'))"
            )
        ).scalar_one()
        can_create = conn.execute(
            text("SELECT has_schema_privilege('andai_scan', 'public', 'CREATE')")
        ).scalar_one()
    assert fn[0] is True and fn[1] == "andai_scan"
    assert fn[2] == ["search_path=public, pg_temp"]
    assert fn[3] == "SETOF uuid"
    assert (fn[4], fn[5]) == (False, True)
    assert columns == {
        "org_id": True, "platform_user_id": False, "key_ciphertext": False, "key_id": False
    }
    assert other_tables == 0
    assert can_create is False


def test_scan_role_cannot_log_in(migrated: Postgres) -> None:
    engine = create_engine(migrated.superuser_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb"
                    " FROM pg_roles WHERE rolname = 'andai_scan'"
                )
            ).one()
    finally:
        engine.dispose()
    assert tuple(row) == (False, False, False, False, False)
    scan = create_engine(migrated.url("andai_scan", "anything"))
    try:
        with pytest.raises(Exception, match="(?i)not permitted to log in|password authentication"):
            with scan.connect():
                pass
    finally:
        scan.dispose()


def test_app_role_cannot_call_the_scan_role_directly(app_engine: Engine) -> None:
    with pytest.raises(ProgrammingError):
        with transaction(app_engine) as tx:
            tx.execute(text("SET ROLE andai_scan"))


# ─── cross-org listing of orgs with open meetings (migration 0004) ────────────────────────────────


def test_open_meeting_orgs_lists_open_meetings_while_the_table_stays_hidden(
    app_engine: Engine, org_a: SeededOrg, org_b: SeededOrg
) -> None:
    done_only = _seed(app_engine)
    with transaction(app_engine, org_id=done_only.org_id) as tx:
        tx.execute(
            text("UPDATE meetings SET status = 'done' WHERE org_id = :o"), {"o": done_only.org_id}
        )

    with transaction(app_engine) as tx:  # andai_app, no app.org_id
        listed = list(tx.scalars(text("SELECT andai_open_meeting_orgs()")))
        direct = tx.execute(text("SELECT count(*) FROM meetings")).scalar_one()
        consent = tx.execute(text("SELECT count(*) FROM consent_records")).scalar_one()

    assert {org_a.org_id, org_b.org_id} <= set(listed)
    assert done_only.org_id not in listed
    assert len(listed) == len(set(listed))  # one row per org, not per meeting
    assert (direct, consent) == (0, 0)


def test_open_meeting_orgs_reveals_org_ids_only(superuser_engine: Engine) -> None:
    with superuser_engine.connect() as conn:
        fn = conn.execute(
            text(
                "SELECT p.prosecdef, r.rolname, p.proconfig, pg_get_function_result(p.oid),"
                " has_function_privilege('public', p.oid, 'EXECUTE'),"
                " has_function_privilege('andai_app', p.oid, 'EXECUTE')"
                " FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner"
                " WHERE p.proname = 'andai_open_meeting_orgs'"
            )
        ).one()
        meeting_columns = [
            name
            for (name,) in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'meetings'"
                )
            )
        ]
        readable = {
            column
            for column in meeting_columns
            if conn.execute(
                text("SELECT has_column_privilege('andai_scan', 'meetings', :c, 'SELECT')"),
                {"c": column},
            ).scalar_one()
        }
        consent_columns = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.column_privileges"
                " WHERE grantee = 'andai_scan' AND table_name = 'consent_records'"
            )
        ).scalar_one()
        can_create = conn.execute(
            text("SELECT has_schema_privilege('andai_scan', 'public', 'CREATE')")
        ).scalar_one()
    assert fn[0] is True and fn[1] == "andai_scan"
    assert fn[2] == ["search_path=public, pg_temp"]
    assert fn[3] == "SETOF uuid"
    assert (fn[4], fn[5]) == (False, True)
    # 0004 (open meetings), 0005 (join statistics) and 0006 (the ingest claim) together; never
    # the link, the title, attendees or people.
    assert readable == {
        "org_id", "status", "platform", "source", "failure_reason", "end_reason", "started_at",
        "created_at", "id", "platform_meeting_id", "transcript_state", "transcript_attempts",
        "transcript_next_at",
    }
    assert consent_columns == 0
    assert can_create is False


def test_a_failed_meeting_needs_a_reason_and_only_a_failed_one_has_one(
    app_engine: Engine, org_a: SeededOrg
) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError, match="failed_has_reason"):
        with transaction(app_engine, org_id=org_a.org_id) as tx:
            tx.execute(
                text("UPDATE meetings SET status = 'failed' WHERE id = :m"),
                {"m": org_a.meeting_id},
            )
    with pytest.raises(IntegrityError, match="failed_has_reason"):
        with transaction(app_engine, org_id=org_a.org_id) as tx:
            tx.execute(
                text("UPDATE meetings SET failure_reason = 'internal_error' WHERE id = :m"),
                {"m": org_a.meeting_id},
            )


def test_one_live_meeting_per_link_and_per_native_id_in_an_org(
    app_engine: Engine, org_a: SeededOrg, org_b: SeededOrg
) -> None:
    from sqlalchemy.exc import IntegrityError

    with transaction(app_engine, org_id=org_a.org_id) as tx:
        key, native = tx.execute(
            text("SELECT join_url_key, native_meeting_id FROM meetings WHERE id = :m"),
            {"m": org_a.meeting_id},
        ).one()

    def insert_like(org: SeededOrg, **overrides: object) -> None:
        values = {
            "id": uuid.uuid4(), "org_id": org.org_id, "requested_by": org.owner_id,
            "platform": "zoom", "join_url": "https://zoom.us/j/1", "join_url_key": key,
            "native_meeting_id": None, "status": "dispatching", **overrides,
        }
        with transaction(app_engine, org_id=org.org_id) as tx:
            tx.execute(insert(Meeting.__table__).values(**values))

    with pytest.raises(IntegrityError, match="uq_meetings_live_join_url_key"):
        insert_like(org_a)
    with pytest.raises(IntegrityError, match="uq_meetings_live_native_id"):
        insert_like(org_a, join_url_key="zoom.us/j/other", native_meeting_id=native)
    # A terminal row of the same link, and the same link in another org, are allowed.
    insert_like(org_a, status="done")
    insert_like(org_b, native_meeting_id=native)


# ─── canary org id, join statistics and system tables (migration 0005) ────────────────────────────


def test_canary_org_id_and_join_stats_are_scan_owned_definer_functions(
    superuser_engine: Engine,
) -> None:
    with superuser_engine.connect() as conn:
        rows = {
            name: tuple(rest)
            for name, *rest in conn.execute(
                text(
                    "SELECT p.proname, p.prosecdef, r.rolname, p.proconfig,"
                    " pg_get_function_result(p.oid),"
                    " has_function_privilege('public', p.oid, 'EXECUTE'),"
                    " has_function_privilege('andai_app', p.oid, 'EXECUTE')"
                    " FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner"
                    " WHERE p.proname IN ('andai_canary_org_id', 'andai_join_stats')"
                )
            ).all()
        }
        org_columns = {
            column
            for (column,) in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'orgs'"
                )
            )
            if conn.execute(
                text("SELECT has_column_privilege('andai_scan', 'orgs', :c, 'SELECT')"),
                {"c": column},
            ).scalar_one()
        }
    path = ["search_path=public, pg_temp"]
    assert rows["andai_canary_org_id"] == (True, "andai_scan", path, "uuid", False, True)
    assert rows["andai_join_stats"] == (
        True, "andai_scan", path,
        "TABLE(platform text, source text, outcome text, reason text, count bigint)", False, True,
    )
    assert org_columns == {"id", "is_canary"}


def test_join_stats_counts_every_org_while_meetings_stay_hidden(
    app_engine: Engine, org_a: SeededOrg, org_b: SeededOrg
) -> None:
    since = datetime.now(UTC) - timedelta(hours=1)
    with transaction(app_engine) as tx:  # andai_app, no app.org_id
        rows = tx.execute(text("SELECT * FROM andai_join_stats(:s)"), {"s": since}).all()
        direct = tx.execute(text("SELECT count(*) FROM meetings")).scalar_one()
    assert sum(row.count for row in rows) >= 2  # org A's and org B's seeded meetings
    assert all(set(row._fields) == {"platform", "source", "outcome", "reason", "count"}
               for row in rows)
    assert direct == 0


def test_system_tables_hold_no_org_data_and_the_scan_role_cannot_read_them(
    superuser_engine: Engine,
) -> None:
    with superuser_engine.connect() as conn:
        for table in SYSTEM_TABLES:
            columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema = 'public' AND table_name = :t"
                    ),
                    {"t": table},
                ).scalars()
            )
            assert columns and not {"org_id", "user_id", "join_url", "email"} & columns, table
            assert conn.execute(
                text("SELECT has_table_privilege('andai_scan', :t, 'SELECT')"), {"t": table}
            ).scalar_one() is False


# ─── andai's transcript copy and the ingest claim (migration 0006) ────────────────────────────────

CLAIM = "SELECT org_id, meeting_id FROM andai_claim_ingest(:n)"


def _set_meeting(engine: Engine, org: SeededOrg, meeting_id: uuid.UUID, **values: object) -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    with transaction(engine, org_id=org.org_id) as tx:
        tx.execute(
            text(f"UPDATE meetings SET {assignments} WHERE id = :m"), {"m": meeting_id, **values}
        )


def _mine(rows: list, org: SeededOrg) -> set[uuid.UUID]:
    return {meeting for org_id, meeting in rows if org_id == org.org_id}


def test_claim_ingest_returns_finished_meetings_of_every_org_while_meetings_stay_hidden(
    app_engine: Engine, org_a: SeededOrg, org_b: SeededOrg
) -> None:
    for org in (org_a, org_b):
        _set_meeting(app_engine, org, org.meeting_id, status="done")
    with transaction(app_engine) as tx:  # andai_app, no app.org_id
        claimed = tx.execute(text(CLAIM), {"n": 1000}).all()
        direct = tx.execute(text("SELECT count(*) FROM meetings")).scalar_one()
        segments = tx.execute(text("SELECT count(*) FROM transcript_segments")).scalar_one()
    assert _mine(claimed, org_a) == {org_a.meeting_id}
    assert _mine(claimed, org_b) == {org_b.meeting_id}
    assert all(set(row._fields) == {"org_id", "meeting_id"} for row in claimed)
    assert (direct, segments) == (0, 0)


def test_claim_ingest_takes_only_terminal_meetings_with_a_platform_id_that_are_due(
    app_engine: Engine, superuser_engine: Engine, org_a: SeededOrg
) -> None:
    def meeting(status: str, **values: object) -> uuid.UUID:
        meeting_id = _seed_meeting(app_engine, org_a.org_id, org_a.owner_id, status=status)
        if values:
            _set_meeting(app_engine, org_a, meeting_id, **values)
        return meeting_id

    done = meeting("done")
    failed = meeting("failed")
    cancelled = meeting("cancelled")
    meeting("done", platform_meeting_id=None)
    meeting("done", transcript_state="stored")
    meeting("done", transcript_state="failed")
    meeting("done", transcript_attempts=8)
    meeting("done", transcript_next_at=datetime.now(UTC) + timedelta(minutes=5))
    meeting("processing")
    retry = meeting("done", transcript_state="pending", transcript_attempts=3,
                    transcript_next_at=datetime.now(UTC) - timedelta(seconds=1))
    # org_a's seeded meeting is still recording

    with transaction(app_engine) as tx:
        first = tx.execute(text(CLAIM), {"n": 1000}).all()
    with transaction(app_engine) as tx:
        again = tx.execute(text(CLAIM), {"n": 1000}).all()

    assert _mine(first, org_a) == {done, failed, cancelled, retry}
    assert _mine(again, org_a) == set()  # leased
    with superuser_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT transcript_state, transcript_attempts, transcript_next_at > now()"
                " FROM meetings WHERE id = ANY(:ids)"
            ),
            {"ids": [done, failed, cancelled, retry]},
        ).all()
    assert sorted(rows) == sorted(
        [("pending", 0, True), ("pending", 0, True), ("pending", 0, True), ("pending", 3, True)]
    )


def test_claim_ingest_respects_the_limit(app_engine: Engine, org_a: SeededOrg) -> None:
    ids = {
        _seed_meeting(app_engine, org_a.org_id, org_a.owner_id, status="done") for _ in range(3)
    }
    with transaction(app_engine) as tx:
        none = tx.execute(text(CLAIM), {"n": 0}).all()
        two = tx.execute(text(CLAIM), {"n": 2}).all()
    with transaction(app_engine) as tx:
        rest = tx.execute(text(CLAIM), {"n": 100_000}).all()
    assert none == [] and len(two) == 2
    assert _mine(two, org_a) | _mine(rest, org_a) == ids
    assert not _mine(two, org_a) & _mine(rest, org_a)


def test_two_claimers_never_take_the_same_meeting(app_engine: Engine, org_a: SeededOrg) -> None:
    ids = {
        _seed_meeting(app_engine, org_a.org_id, org_a.owner_id, status="done") for _ in range(4)
    }
    with app_engine.connect() as first:
        held = first.execute(text(CLAIM), {"n": 1000}).all()  # rows stay locked: no commit yet
        with app_engine.connect() as second:
            other = second.execute(text(CLAIM), {"n": 1000}).all()
            second.commit()
        first.commit()
    assert _mine(held, org_a) == ids
    assert _mine(other, org_a) == set()


def test_claim_ingest_is_a_scan_owned_definer_function(superuser_engine: Engine) -> None:
    with superuser_engine.connect() as conn:
        fn = conn.execute(
            text(
                "SELECT p.prosecdef, r.rolname, p.proconfig, pg_get_function_result(p.oid),"
                " has_function_privilege('public', p.oid, 'EXECUTE'),"
                " has_function_privilege('andai_app', p.oid, 'EXECUTE')"
                " FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner"
                " WHERE p.proname = 'andai_claim_ingest'"
            )
        ).one()
        columns = [
            name
            for (name,) in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'meetings'"
                )
            )
        ]
        updatable = {
            column
            for column in columns
            if conn.execute(
                text("SELECT has_column_privilege('andai_scan', 'meetings', :c, 'UPDATE')"),
                {"c": column},
            ).scalar_one()
        }
        segments = conn.execute(
            text(
                "SELECT has_table_privilege('andai_scan', 'transcript_segments',"
                " 'SELECT, INSERT, UPDATE, DELETE')"
            )
        ).scalar_one()
        segment_columns = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.column_privileges"
                " WHERE grantee = 'andai_scan' AND table_name = 'transcript_segments'"
            )
        ).scalar_one()
    assert fn[0] is True and fn[1] == "andai_scan"
    assert fn[2] == ["search_path=public, pg_temp"]
    assert fn[3] == "TABLE(org_id uuid, meeting_id uuid)"
    assert (fn[4], fn[5]) == (False, True)
    assert updatable == {"transcript_state", "transcript_attempts", "transcript_next_at"}
    assert (segments, segment_columns) == (False, 0)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT join_url FROM meetings",
        "SELECT title FROM meetings",
        "SELECT attendees FROM meetings",
        "SELECT requested_by FROM meetings",
        "SELECT settings_snapshot FROM meetings",
        "SELECT * FROM meetings",
        "SELECT * FROM transcript_segments",
        "SELECT text_raw FROM transcript_segments",
        "UPDATE meetings SET status = 'done'",
        "UPDATE meetings SET transcript_at = now()",
        "UPDATE meetings SET attendees = '[]'::jsonb",
        "DELETE FROM meetings",
        "DELETE FROM transcript_segments",
        "INSERT INTO transcript_segments (meeting_id, id, org_id, seq, speaker, text_raw,"
        " text_canonical, script, search_text) SELECT id, 'x', org_id, 0, '', '', '', 'latn', ''"
        " FROM meetings LIMIT 1",
    ],
)
def test_the_scan_role_cannot_read_or_write_beyond_its_grants(
    superuser_engine: Engine, org_a: SeededOrg, statement: str
) -> None:
    with superuser_engine.connect() as conn:
        conn.execute(text("SET LOCAL ROLE andai_scan"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text(statement))


def test_the_scan_role_can_do_what_the_claim_needs(
    superuser_engine: Engine, org_a: SeededOrg
) -> None:
    with superuser_engine.connect() as conn:  # rolled back on close
        conn.execute(text("SET LOCAL ROLE andai_scan"))
        seen = conn.execute(
            text(
                "SELECT id, org_id, status, platform_meeting_id, transcript_state,"
                " transcript_attempts, transcript_next_at FROM meetings WHERE id = :m"
            ),
            {"m": org_a.meeting_id},
        ).all()
        updated = conn.execute(
            text(
                "UPDATE meetings SET transcript_state = 'pending', transcript_attempts = 1,"
                " transcript_next_at = now() WHERE id = :m"
            ),
            {"m": org_a.meeting_id},
        ).rowcount
    assert len(seen) == 1 and updated == 1
