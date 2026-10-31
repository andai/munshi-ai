"""Deleting a meeting (PORT-04), `DELETE /api/v1/meetings/{id}`: who may (`.work/AUTH_MATRIX.md`),
that the meeting is gone from every read path at once, the `meeting.delete` audit event in the same
transaction, the live-meeting refusal, a second delete, and the link afterwards: free again, except
for a meeting participants removed the bot from, which keeps refusing the link for 12 hours (D-38)
after the meeting is deleted and purged. The purge itself is `services/worker/tests/
test_worker_deletion.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from andai_core.db.models import ConsentRecord
from andai_core.db.session import transaction
from andai_core.meetings import deletion, mom
from conftest import (
    CALLERS,
    DELETERS,
    READERS,
    AccessOrg,
    access_org,
    give_principal,
    mom_body,
    seed_segments,
    zoom_link,
)

JOIN = "/api/v1/meetings/join"


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> AccessOrg:
    w = access_org(app_engine)
    give_principal(w.harness, app_engine, w.org_id)
    return w


def _path(meeting_id: uuid.UUID | str) -> str:
    return f"/api/v1/meetings/{meeting_id}"


def _refused(response) -> bool:
    return response.status_code == 404 and response.json()["code"] == "no_such_meeting"


def _row(superuser_engine: Engine, meeting_id: uuid.UUID | str) -> dict | None:
    with superuser_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT deleted_at, deleted_by, deletion_state, deletion_attempts"
                " FROM meetings WHERE id = :m"
            ),
            {"m": meeting_id},
        ).mappings().first()
    return None if row is None else dict(row)


def _audit(superuser_engine: Engine, meeting_id: uuid.UUID | str) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT action, actor_user_id FROM audit_events WHERE target = :t"
                    " AND action LIKE 'meeting.%' AND action <> 'meeting.dispatch'"
                    " ORDER BY at"
                ),
                {"t": f"meeting:{meeting_id}"},
            )
        ]


def _listed(world: AccessOrg, caller: str, meeting_id: uuid.UUID) -> bool:
    body = world.client(caller).get("/api/v1/meetings", params={"limit": 50}).json()
    return any(
        item["id"] == str(meeting_id)
        for section in body["sections"].values()
        for item in section["items"]
    )


def _found(world: AccessOrg, caller: str, query: str) -> tuple[int, set[str]]:
    body = world.client(caller).get("/api/v1/search", params={"q": query}).json()
    return body["total"], {r["id"] for r in body["results"]}


# ─── who may delete ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", CALLERS)
def test_only_the_requester_admins_and_owners_may_delete(
    world: AccessOrg, superuser_engine: Engine, caller: str
) -> None:
    meeting_id = world.meeting()
    now = world.harness.clock()

    response = world.client(caller).delete(_path(meeting_id))

    if caller in DELETERS:
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["id"] == str(meeting_id)
        assert datetime.fromisoformat(body["purge_by"]) == now + timedelta(minutes=15)
        row = _row(superuser_engine, meeting_id)
        assert row["deleted_at"] == now and row["deletion_state"] == "pending"
        assert row["deleted_by"] == world.people[caller].user_id
        assert _audit(superuser_engine, meeting_id) == [
            ("meeting.delete", world.people[caller].user_id)
        ]
    else:
        # A lead or a grantee may read it, but for deleting it does not exist.
        assert _refused(response), response.text
        assert _row(superuser_engine, meeting_id)["deletion_state"] == "none"
        assert _audit(superuser_engine, meeting_id) == []
        assert world.client("requester").get(_path(meeting_id)).status_code == 200


# ─── hidden at once ───────────────────────────────────────────────────────────────────────────────


def test_a_deleted_meeting_is_gone_from_every_read_path_at_once(world: AccessOrg) -> None:
    tag = uuid.uuid4().hex[:10]
    meeting_id = world.meeting(title=f"Budget review {tag}")
    segments = seed_segments(world.engine, world.org_id, meeting_id, [f"kal {tag} final karenge"])
    with transaction(world.engine, org_id=world.org_id) as tx:
        mom.seed_version(tx, world.org_id, meeting_id, mom_body(segments * 3))
    reads = ("", "/transcript", "/live", "/mom")

    for caller in READERS:
        client = world.client(caller)
        assert all(client.get(_path(meeting_id) + r).status_code == 200 for r in reads), caller
        assert _listed(world, caller, meeting_id), caller
        assert _found(world, caller, tag) == (1, {str(meeting_id)}), caller
    assert world.client("requester").get(_path(meeting_id) + "/shares").status_code == 200

    assert world.client("requester").delete(_path(meeting_id)).status_code == 202

    for caller in READERS:
        client = world.client(caller)
        for r in reads:
            assert _refused(client.get(_path(meeting_id) + r)), (caller, r)
        assert not _listed(world, caller, meeting_id), caller
        assert _found(world, caller, tag) == (0, set()), caller
    requester = world.client("requester")
    assert _refused(requester.get(_path(meeting_id) + "/shares"))
    assert _refused(
        requester.post(
            _path(meeting_id) + "/shares",
            json={"grantee_type": "user", "grantee_id": str(world.people["unrelated"].user_id)},
        )
    )
    assert _refused(requester.post(_path(meeting_id) + "/stop"))


def test_a_second_delete_is_404(world: AccessOrg, superuser_engine: Engine) -> None:
    meeting_id = world.meeting()
    assert world.client("requester").delete(_path(meeting_id)).status_code == 202

    again = world.client("admin").delete(_path(meeting_id))

    assert _refused(again), again.text
    assert _audit(superuser_engine, meeting_id) == [
        ("meeting.delete", world.people["requester"].user_id)
    ]


def test_another_orgs_owner_cannot_delete(world: AccessOrg, superuser_engine: Engine) -> None:
    meeting_id = world.meeting()

    assert _refused(world.client("other_org").delete(_path(meeting_id)))
    assert _row(superuser_engine, meeting_id)["deletion_state"] == "none"


# ─── audited in the same transaction ──────────────────────────────────────────────────────────────


def test_the_audit_event_and_the_hiding_commit_together(
    world: AccessOrg, superuser_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    meeting_id = world.meeting()

    def broken(*args, **kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(deletion.audit, "record", broken)
    with pytest.raises(RuntimeError, match="audit store unavailable"):
        world.client("requester").delete(_path(meeting_id))
    monkeypatch.undo()

    # The failed audit write took the hiding with it: still there for everyone, nothing audited.
    assert _row(superuser_engine, meeting_id)["deletion_state"] == "none"
    assert _row(superuser_engine, meeting_id)["deleted_at"] is None
    assert world.client("requester").get(_path(meeting_id)).status_code == 200
    assert _audit(superuser_engine, meeting_id) == []


# ─── a live meeting ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status", ["scheduled", "dispatching", "awaiting_admission", "recording", "processing"]
)
def test_a_meeting_the_bot_is_still_in_cannot_be_deleted(
    world: AccessOrg, superuser_engine: Engine, status: str
) -> None:
    meeting_id = world.meeting(status=status)

    response = world.client("owner").delete(_path(meeting_id))

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "meeting_live"
    assert _row(superuser_engine, meeting_id)["deletion_state"] == "none"
    assert world.client("requester").get(_path(meeting_id)).status_code == 200
    assert _audit(superuser_engine, meeting_id) == []


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_every_ended_meeting_can_be_deleted(world: AccessOrg, status: str) -> None:
    meeting_id = world.meeting(status=status)

    assert world.client("requester").delete(_path(meeting_id)).status_code == 202


# ─── the link after deletion ──────────────────────────────────────────────────────────────────────


def _send(world: AccessOrg, link: str):
    return world.client("requester").post(JOIN, json={"join_url": link})


def _end(world: AccessOrg, meeting_id: str, *, removed_hours_ago: float | None = None) -> None:
    """The meeting ended on the platform and in andai; with `removed_hours_ago`, participants
    removed the bot that long before the harness clock."""
    gateway = world.harness.gateway
    with transaction(world.engine, org_id=world.org_id) as tx:
        platform_id = tx.execute(
            text("SELECT platform_meeting_id FROM meetings WHERE id = :m"), {"m": meeting_id}
        ).scalar_one()
        tx.execute(
            text(
                "UPDATE meetings SET status = 'done', end_reason = :r, ended_at = now()"
                " WHERE id = :m"
            ),
            {"m": meeting_id, "r": None if removed_hours_ago is None else "evicted"},
        )
        if removed_hours_ago is not None:
            at = world.harness.clock() - timedelta(hours=removed_hours_ago)
            tx.add(
                ConsentRecord(
                    meeting_id=uuid.UUID(meeting_id),
                    org_id=world.org_id,
                    bot_display_name="munshi.ai (recording)",
                    disclosure_text="notice",
                    privacy_notice_url="http://portal.test/privacy",
                    disclosure_channel="chat",
                    admitted_at=at - timedelta(minutes=5),
                    removed_at=at,
                    removed_by="participant",
                )
            )
    gateway.set(platform_id, status="completed")


def _purge(world: AccessOrg, meeting_id: str):
    s = world.harness.services
    return asyncio.run(
        deletion.purge(
            world.engine, s.keyring, s.gateway, world.org_id, uuid.UUID(meeting_id),
            clock=world.harness.clock,
        )
    )


def _removals(superuser_engine: Engine, org_id: uuid.UUID) -> list[dict]:
    with superuser_engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text("SELECT * FROM meeting_removals WHERE org_id = :o"), {"o": org_id}
            ).mappings()
        ]


def test_the_link_can_be_sent_again_once_its_meeting_is_deleted(world: AccessOrg) -> None:
    link = zoom_link()
    first = _send(world, link)
    assert first.status_code == 201, first.text
    _end(world, first.json()["id"])
    assert world.client("requester").delete(_path(first.json()["id"])).status_code == 202

    hidden = _send(world, link)  # deleted, not yet purged
    assert hidden.status_code == 201, hidden.text
    _end(world, hidden.json()["id"])
    assert world.client("requester").delete(_path(hidden.json()["id"])).status_code == 202
    assert _purge(world, first.json()["id"]).state == "purged"
    assert _purge(world, hidden.json()["id"]).state == "purged"

    again = _send(world, link)  # both purged
    assert again.status_code == 201, again.text
    assert len({first.json()["id"], hidden.json()["id"], again.json()["id"]}) == 3


def test_a_removed_meeting_refuses_its_link_for_12_hours_after_delete_and_purge(
    world: AccessOrg, superuser_engine: Engine
) -> None:
    link = zoom_link()
    sent = _send(world, link)
    meeting_id = sent.json()["id"]
    _end(world, meeting_id, removed_hours_ago=1)
    removed_at = world.harness.clock() - timedelta(hours=1)

    assert world.client("requester").delete(_path(meeting_id)).status_code == 202
    hidden = _send(world, link)
    assert (hidden.status_code, hidden.json()["code"]) == (409, "removed_by_participant")

    assert _purge(world, meeting_id).state == "purged"
    with superuser_engine.connect() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM consent_records WHERE meeting_id = :m"), {"m": meeting_id}
        ).scalar_one()
    assert left == 0  # the consent record went with the meeting
    markers = [
        m for m in _removals(superuser_engine, world.org_id) if m["removed_at"] == removed_at
    ]
    assert len(markers) == 1
    # The marker holds the org, the link key and the time; nothing of the meeting itself.
    assert set(markers[0]) == {"id", "org_id", "join_url_key", "removed_at"}
    assert markers[0]["join_url_key"] in link and "?" not in markers[0]["join_url_key"]

    purged = _send(world, link)
    by_admin = world.client("admin").post(JOIN, json={"join_url": link})
    assert (purged.status_code, purged.json()["code"]) == (409, "removed_by_participant")
    assert by_admin.json()["code"] == "removed_by_participant"

    world.harness.clock.advance(hours=11, minutes=1)  # 12 h 1 min after the removal
    try:
        allowed = _send(world, link)
        assert allowed.status_code == 201, allowed.text
    finally:
        world.harness.clock.advance(hours=-11, minutes=-1)


def test_an_old_removal_leaves_no_marker(world: AccessOrg, superuser_engine: Engine) -> None:
    link = zoom_link()
    meeting_id = _send(world, link).json()["id"]
    _end(world, meeting_id, removed_hours_ago=13)
    before = len(_removals(superuser_engine, world.org_id))

    assert world.client("requester").delete(_path(meeting_id)).status_code == 202
    assert _purge(world, meeting_id).state == "purged"

    assert len(_removals(superuser_engine, world.org_id)) == before
    assert _send(world, link).status_code == 201
