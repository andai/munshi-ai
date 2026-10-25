"""The audit trail is written in the caller's transaction and is append-only for the app role."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from andai_core import audit
from andai_core.db.session import transaction
from conftest import bootstrap_org, new_user


@pytest.fixture
def org(app_engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    owner_id = new_user(app_engine)
    return bootstrap_org(app_engine, owner_id), owner_id


def _event(superuser_engine: Engine, event_id: uuid.UUID):
    with superuser_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT org_id, actor_user_id, action, target, host(ip), at FROM audit_events"
                " WHERE id = :id"
            ),
            {"id": event_id},
        ).one_or_none()


def test_record_writes_an_event(
    app_engine: Engine, superuser_engine: Engine, org: tuple[uuid.UUID, uuid.UUID]
) -> None:
    org_id, owner_id = org
    with transaction(app_engine, org_id=org_id, user_id=owner_id) as tx:
        event_id = audit.record(tx, org_id, owner_id, "user.invite", "user:someone", "203.0.113.7")
    row = _event(superuser_engine, event_id)
    assert row is not None
    assert row[:5] == (org_id, owner_id, "user.invite", "user:someone", "203.0.113.7")
    assert row[5] is not None


def test_record_rolls_back_with_the_callers_transaction(
    app_engine: Engine, superuser_engine: Engine, org: tuple[uuid.UUID, uuid.UUID]
) -> None:
    org_id, owner_id = org
    with pytest.raises(RuntimeError, match="action failed"):
        with transaction(app_engine, org_id=org_id) as tx:
            event_id = audit.record(tx, org_id, owner_id, "settings.update", "org:settings")
            raise RuntimeError("action failed")
    assert _event(superuser_engine, event_id) is None


def test_record_refuses_another_orgs_event(
    app_engine: Engine, org: tuple[uuid.UUID, uuid.UUID]
) -> None:
    org_id, owner_id = org
    other_org = bootstrap_org(app_engine, new_user(app_engine))
    with pytest.raises(ProgrammingError, match="row-level security"):
        with transaction(app_engine, org_id=other_org) as tx:
            audit.record(tx, org_id, owner_id, "user.invite", "user:someone")


def test_record_requires_action_and_target(app_engine: Engine, org) -> None:
    org_id, owner_id = org
    with transaction(app_engine, org_id=org_id) as tx:
        with pytest.raises(ValueError):
            audit.record(tx, org_id, owner_id, "", "user:someone")
        with pytest.raises(ValueError):
            audit.record(tx, org_id, owner_id, "user.invite", "")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET action = 'tampered' WHERE id = :id",
        "DELETE FROM audit_events WHERE id = :id",
        "TRUNCATE audit_events",
        "SELECT * FROM audit_events WHERE id = :id",
    ],
    ids=["update", "delete", "truncate", "select"],
)
def test_app_role_cannot_change_or_read_audit_rows(
    app_engine: Engine,
    superuser_engine: Engine,
    org: tuple[uuid.UUID, uuid.UUID],
    statement: str,
) -> None:
    org_id, owner_id = org
    with transaction(app_engine, org_id=org_id) as tx:
        event_id = audit.record(tx, org_id, owner_id, "share.grant", "meeting:1")
    before = _event(superuser_engine, event_id)

    with pytest.raises(ProgrammingError, match="permission denied for table audit_events"):
        with transaction(app_engine, org_id=org_id, user_id=owner_id) as tx:
            tx.execute(text(statement), {"id": event_id})

    assert _event(superuser_engine, event_id) == before
