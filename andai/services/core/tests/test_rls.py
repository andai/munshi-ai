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
    AuditEvent,
    Invite,
    Org,
    OrgMembership,
    PlatformPrincipal,
    Team,
    TeamMembership,
    User,
)
from andai_core.db.session import transaction
from conftest import Postgres

RLS_ERROR = "row-level security"

# Org-scoped tables the app role can read, with the column that carries the org.
READABLE = {
    "orgs": "id",
    "org_memberships": "org_id",
    "teams": "org_id",
    "team_memberships": "org_id",
    "invites": "org_id",
    "platform_principals": "org_id",
}


def _token_hash() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _new_user(engine: Engine) -> uuid.UUID:
    user_id = uuid.uuid4()
    with transaction(engine) as tx:  # users are global: no org context needed
        tx.add(User(id=user_id, email=f"{user_id.hex}@example.test", display_name="Test User"))
    return user_id


def _bootstrap_org(engine: Engine, owner_id: uuid.UUID) -> uuid.UUID:
    """The bootstrap path: generate the org id, set app.org_id, insert org + owner membership."""
    org_id = uuid.uuid4()
    with transaction(engine, org_id=org_id) as tx:
        tx.add(Org(id=org_id, name=f"Org {org_id.hex[:8]}", slug=f"org-{org_id.hex}"))
        tx.flush()
        tx.add(OrgMembership(org_id=org_id, user_id=owner_id, role="owner"))
    return org_id


@dataclass(frozen=True)
class SeededOrg:
    org_id: uuid.UUID
    owner_id: uuid.UUID
    team_id: uuid.UUID
    invite_id: uuid.UUID
    invite_token_hash: str


def _seed(engine: Engine) -> SeededOrg:
    owner_id = _new_user(engine)
    org_id = _bootstrap_org(engine, owner_id)
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
    return SeededOrg(org_id, owner_id, team_id, invite_id, token_hash)


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
    for table in GLOBAL_TABLES:
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
    other_member = _new_user(app_engine)
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
    owner_id = _new_user(app_engine)
    org_id = _bootstrap_org(app_engine, owner_id)
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
