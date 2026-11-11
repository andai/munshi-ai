"""The audit trail is append-only for every role (0017), and an org reads only its own trail
through `andai_audit_page`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from andai_core import audit
from andai_core.db.session import transaction
from conftest import bootstrap_org, new_user

APPEND_ONLY = "audit_events is append-only"
CHANGES = {
    "update": "UPDATE audit_events SET action = 'tampered' WHERE id = :id",
    "delete": "DELETE FROM audit_events WHERE id = :id",
    "truncate": "TRUNCATE audit_events",
}
PAGE_SQL = (
    "SELECT * FROM andai_audit_page(:f, :t, CAST(:a AS text[]), CAST(:ba AS timestamptz),"
    " CAST(:bi AS uuid), :l)"
)
BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
FAR_PAST = datetime(2000, 1, 1, tzinfo=UTC)
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)


@pytest.fixture
def org(app_engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    owner_id = new_user(app_engine)
    return bootstrap_org(app_engine, owner_id), owner_id


def _record(engine: Engine, org_id: uuid.UUID, actor: uuid.UUID, action: str = "share.grant"):
    with transaction(engine, org_id=org_id) as tx:
        return audit.record(tx, org_id, actor, action, f"meeting:{uuid.uuid4()}", "203.0.113.7")


def _row(superuser_engine: Engine, event_id: uuid.UUID):
    with superuser_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT org_id, actor_user_id, action, target, at FROM audit_events"
                " WHERE id = :i"
            ),
            {"i": event_id},
        ).one_or_none()


def _seed(superuser_engine: Engine, org_id: uuid.UUID, rows: list[tuple[datetime, str]]):
    """Events at chosen times (the superuser sets `at`; the app role's inserts take now())."""
    ids = []
    with superuser_engine.begin() as conn:
        for at, action in rows:
            event_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO audit_events (id, org_id, actor_user_id, action, target, at)"
                    " VALUES (:i, :o, NULL, :a, :t, :at)"
                ),
                {"i": event_id, "o": org_id, "a": action, "t": f"org:{org_id}", "at": at},
            )
            ids.append(event_id)
    return ids


def _page(
    engine: Engine,
    org_id: uuid.UUID | None,
    *,
    start: datetime = FAR_PAST,
    end: datetime = FAR_FUTURE,
    actions: list[str] | None = None,
    before: tuple[datetime, uuid.UUID] | None = None,
    limit: int | None = 50,
):
    params = {
        "f": start,
        "t": end,
        "a": actions,
        "ba": None if before is None else before[0],
        "bi": None if before is None else before[1],
        "l": limit,
    }
    with transaction(engine, org_id=org_id) as tx:
        return tx.execute(text(PAGE_SQL), params).all()


# ─── append-only ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_owner_role_cannot_change_audit_rows(
    app_engine: Engine, owner_engine: Engine, superuser_engine: Engine, org, change: str
) -> None:
    org_id, owner_id = org
    event_id = _record(app_engine, org_id, owner_id)
    before = _row(superuser_engine, event_id)
    with pytest.raises(DBAPIError, match=APPEND_ONLY):
        with owner_engine.begin() as conn:
            # In the org's context, so row-level security does not hide the row from the change.
            conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
            conn.execute(text(CHANGES[change]), {"id": event_id})
    assert _row(superuser_engine, event_id) == before


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_a_superuser_is_stopped_too(
    app_engine: Engine, superuser_engine: Engine, org, change: str
) -> None:
    org_id, owner_id = org
    event_id = _record(app_engine, org_id, owner_id)
    before = _row(superuser_engine, event_id)
    with pytest.raises(DBAPIError, match=APPEND_ONLY):
        with superuser_engine.begin() as conn:
            conn.execute(text(CHANGES[change]), {"id": event_id})
    assert _row(superuser_engine, event_id) == before


def test_a_cascading_truncate_is_stopped(
    app_engine: Engine, superuser_engine: Engine, org
) -> None:
    org_id, owner_id = org
    event_id = _record(app_engine, org_id, owner_id)
    with pytest.raises(DBAPIError, match=APPEND_ONLY):
        with superuser_engine.begin() as conn:
            conn.execute(text("TRUNCATE orgs CASCADE"))
    assert _row(superuser_engine, event_id) is not None


@pytest.mark.parametrize("role", ["andai_app", "andai_scan"])
@pytest.mark.parametrize("change", sorted(CHANGES))
def test_app_and_scan_roles_cannot_change_audit_rows(
    app_engine: Engine, superuser_engine: Engine, org, role: str, change: str
) -> None:
    """Neither role holds UPDATE, DELETE or TRUNCATE, so Postgres refuses before the trigger."""
    org_id, owner_id = org
    event_id = _record(app_engine, org_id, owner_id)
    before = _row(superuser_engine, event_id)
    with pytest.raises(DBAPIError, match="permission denied for table audit_events"):
        with superuser_engine.begin() as conn:
            conn.execute(text(f"SET LOCAL ROLE {role}"))
            conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
            conn.execute(text(CHANGES[change]), {"id": event_id})
    assert _row(superuser_engine, event_id) == before


def test_the_app_role_still_inserts(app_engine: Engine, superuser_engine: Engine, org) -> None:
    org_id, owner_id = org
    event_id = _record(app_engine, org_id, owner_id, "login.success")
    row = _row(superuser_engine, event_id)
    assert row is not None and row[:3] == (org_id, owner_id, "login.success")


def test_the_app_role_still_cannot_select_the_table(app_engine: Engine, org) -> None:
    org_id, _ = org
    with pytest.raises(DBAPIError, match="permission denied for table audit_events"):
        with transaction(app_engine, org_id=org_id) as tx:
            tx.execute(text("SELECT count(*) FROM audit_events"))


# ─── andai_audit_page ─────────────────────────────────────────────────────────────────────────────


def test_page_returns_only_the_context_orgs_rows(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    mine = bootstrap_org(app_engine, new_user(app_engine))
    other = bootstrap_org(app_engine, new_user(app_engine))
    my_ids = _seed(superuser_engine, mine, [(BASE, "share.grant"), (BASE, "share.revoke")])
    _seed(superuser_engine, other, [(BASE, "share.grant")])

    rows = _page(app_engine, mine)
    assert {row.id for row in rows} == set(my_ids)
    assert _page(app_engine, None) == []


def test_page_columns(app_engine: Engine, org) -> None:
    org_id, owner_id = org
    event_id = _record(app_engine, org_id, owner_id, "share.revoke")
    [row] = [r for r in _page(app_engine, org_id) if r.id == event_id]
    assert (row.actor_user_id, row.action, str(row.ip)) == (owner_id, "share.revoke", "203.0.113.7")
    assert row.target.startswith("meeting:")
    assert row.at.tzinfo is not None


def test_page_filters_by_time_and_action(app_engine: Engine, superuser_engine: Engine) -> None:
    org_id = bootstrap_org(app_engine, new_user(app_engine))
    early, middle, late = _seed(
        superuser_engine,
        org_id,
        [
            (BASE, "share.grant"),
            (BASE + timedelta(days=1), "share.revoke"),
            (BASE + timedelta(days=2), "share.grant"),
        ],
    )
    window = _page(
        app_engine, org_id, start=BASE + timedelta(days=1), end=BASE + timedelta(days=2)
    )
    assert [row.id for row in window] == [middle]  # `to` is exclusive
    grants = _page(app_engine, org_id, actions=["share.grant"])
    assert [row.id for row in grants] == [late, early]
    assert _page(app_engine, org_id, actions=["meeting.delete"]) == []


def test_page_is_newest_first_and_keyset_pages_without_duplicates(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    org_id = bootstrap_org(app_engine, new_user(app_engine))
    # Ties on `at` are ordered by id.
    times = [BASE + timedelta(minutes=i // 3) for i in range(25)]
    seeded = set(_seed(superuser_engine, org_id, [(at, "share.grant") for at in times]))

    seen: list[uuid.UUID] = []
    before = None
    while True:
        rows = _page(app_engine, org_id, before=before, limit=7)
        if not rows:
            break
        keys = [(row.at, row.id) for row in rows]
        assert keys == sorted(keys, reverse=True)
        seen.extend(row.id for row in rows)
        before = keys[-1]
    assert len(seen) == len(set(seen)) == 25
    assert set(seen) == seeded


@pytest.mark.parametrize(("asked", "got"), [(500, 100), (0, 1), (-3, 1), (None, 100)])
def test_page_limit_is_clamped(
    app_engine: Engine, superuser_engine: Engine, asked: int | None, got: int
) -> None:
    org_id = bootstrap_org(app_engine, new_user(app_engine))
    rows = [(BASE + timedelta(seconds=i), "share.grant") for i in range(120)]
    _seed(superuser_engine, org_id, rows)
    assert len(_page(app_engine, org_id, limit=asked)) == got


def test_page_function_belongs_to_the_scan_role(superuser_engine: Engine) -> None:
    with superuser_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig, p.provolatile,"
                " has_function_privilege('public', p.oid, 'EXECUTE'),"
                " has_function_privilege('andai_app', p.oid, 'EXECUTE')"
                " FROM pg_proc p WHERE p.proname = 'andai_audit_page'"
            )
        ).one()
    assert tuple(row) == (True, "andai_scan", ["search_path=public, pg_temp"], "s", False, True)
