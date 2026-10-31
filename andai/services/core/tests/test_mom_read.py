"""Minutes of meeting, read only (PORT-02, D-43): `GET /api/v1/meetings/{id}/mom`, the latest
version, its evidence ids, and the title writer search relies on.

Minutes are seeded here through `mom.seed_version`, the only writer in this phase; Phase 6 writes
them from the MoM pipeline.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, delete, select, text

from andai_core.db.models import Meeting, MomDocument
from andai_core.db.session import transaction
from andai_core.meetings import mom
from andai_core.meetings.title import set_title
from andai_core.text import fold
from conftest import CALLERS, READERS, AccessOrg, access_org, seed_segments
from conftest import mom_body as body

LINES = [
    "कल तक deadline है",
    ("Ravi", "theek hai, main kar dunga"),
    "क्या हम budget review करें?",
]


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> AccessOrg:
    w = access_org(app_engine)
    w.meeting_id = w.meeting()
    w.segments = seed_segments(app_engine, w.org_id, w.meeting_id, LINES)  # type: ignore[attr-defined]
    with transaction(app_engine, org_id=w.org_id) as tx:
        mom.seed_version(tx, w.org_id, w.meeting_id, body(w.segments, summary="first draft"))  # type: ignore[attr-defined]
        mom.seed_version(tx, w.org_id, w.meeting_id, body(w.segments))  # type: ignore[attr-defined]
    return w


def _path(meeting_id: uuid.UUID) -> str:
    return f"/api/v1/meetings/{meeting_id}/mom"


# ─── the route ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", CALLERS)
def test_minutes_are_read_by_the_meetings_readers_only(world: AccessOrg, caller: str) -> None:
    response = world.client(caller).get(_path(world.meeting_id))

    if caller in READERS:
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 2
    else:
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "no_such_meeting"


def test_the_latest_version_wins(world: AccessOrg) -> None:
    doc = world.client("requester").get(_path(world.meeting_id)).json()

    assert doc["meetingId"] == str(world.meeting_id)
    assert doc["version"] == 2
    assert doc["summary"] == "Budget review kal tak hoga."
    with transaction(world.engine, org_id=world.org_id) as tx:
        latest = mom.latest(tx, world.meeting_id)
        assert latest is not None and latest.version == 2
        versions = tx.scalars(
            select(MomDocument.version).where(MomDocument.meeting_id == world.meeting_id)
        ).all()
    assert sorted(versions) == [1, 2]


def test_the_document_has_the_spec_shape_and_cites_andai_segment_ids(world: AccessOrg) -> None:
    doc = world.client("user_grantee").get(_path(world.meeting_id)).json()

    assert set(doc) == {
        "meetingId", "version", "templateId", "outputLanguage", "summary", "decisions",
        "actionItems", "topics", "risks", "llmProfileId", "model", "generatedAt",
    }  # fmt: skip
    transcript = world.client("user_grantee").get(
        f"/api/v1/meetings/{world.meeting_id}/transcript"
    ).json()
    stored = {s["id"] for s in transcript["segments"]}
    cited = {
        e
        for section in ("decisions", "actionItems", "topics", "risks")
        for entry in doc[section]
        for e in entry["evidence"]
    }
    assert cited and cited <= stored
    assert [a["owner"] for a in doc["actionItems"]] == ["Ravi", "unassigned"]
    assert doc["actionItems"][0]["due"] == "2026-11-01"
    assert "due" not in doc["actionItems"][1]


def test_a_readable_meeting_without_minutes_is_404_no_minutes(world: AccessOrg) -> None:
    bare = world.meeting()

    response = world.client("requester").get(_path(bare))

    assert response.status_code == 404
    assert response.json()["code"] == "no_minutes"
    assert world.client("unrelated").get(_path(bare)).json()["code"] == "no_such_meeting"


def test_a_revoked_share_loses_the_minutes_on_the_next_request(world: AccessOrg) -> None:
    from andai_core.db.models import MeetingShare

    shared = world.meeting()
    ids = seed_segments(world.engine, world.org_id, shared, LINES)
    with transaction(world.engine, org_id=world.org_id) as tx:
        mom.seed_version(tx, world.org_id, shared, body(ids))
    grantee = world.client("user_grantee")
    assert grantee.get(_path(shared)).status_code == 200

    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(
            delete(MeetingShare).where(
                MeetingShare.meeting_id == shared,
                MeetingShare.grantee_id == world.people["user_grantee"].user_id,
            )
        )

    assert grantee.get(_path(shared)).json()["code"] == "no_such_meeting"


# ─── the store ────────────────────────────────────────────────────────────────────────────────────


def test_evidence_ids_must_be_segments_of_the_meeting(world: AccessOrg) -> None:
    other = world.meeting()
    other_ids = seed_segments(world.engine, world.org_id, other, ["kisi aur meeting ki baat"])
    target = world.meeting()
    ids = seed_segments(world.engine, world.org_id, target, LINES)

    for evidence in (["seg_does_not_exist", *ids], [other_ids[0], *ids]):
        with pytest.raises(mom.MomError, match="evidence"):
            with transaction(world.engine, org_id=world.org_id) as tx:
                mom.seed_version(tx, world.org_id, target, body(evidence))
    with transaction(world.engine, org_id=world.org_id) as tx:
        assert mom.latest(tx, target) is None


def test_an_action_item_needs_evidence(world: AccessOrg) -> None:
    target = world.meeting()
    ids = seed_segments(world.engine, world.org_id, target, LINES)
    doc = body(ids)
    doc["actionItems"][0]["evidence"] = []

    with pytest.raises(mom.MomError):
        with transaction(world.engine, org_id=world.org_id) as tx:
            mom.seed_version(tx, world.org_id, target, doc)


def test_a_body_outside_the_schema_is_refused(world: AccessOrg) -> None:
    target = world.meeting()
    ids = seed_segments(world.engine, world.org_id, target, LINES)

    for doc in (
        body(ids, outputLanguage="fr"),
        body(ids, nextSteps=["not a SPEC field"]),
        {k: v for k, v in body(ids).items() if k != "summary"},
    ):
        with pytest.raises(mom.MomError):
            with transaction(world.engine, org_id=world.org_id) as tx:
                mom.seed_version(tx, world.org_id, target, doc)


def test_search_text_is_the_fold_of_the_spec_text_fields(world: AccessOrg) -> None:
    with transaction(world.engine, org_id=world.org_id) as tx:
        row = mom.latest(tx, world.meeting_id)
    assert row is not None
    doc = row.body
    pieces = [
        doc["summary"],
        *(d["text"] for d in doc["decisions"]),
        *(a["text"] for a in doc["actionItems"]),
        *(t["title"] for t in doc["topics"]),
        *(r["text"] for r in doc["risks"]),
    ]
    assert row.search_text == fold(" ".join(pieces))
    assert (row.template_id, row.output_language) == ("standard", "hinglish")


def test_minutes_go_with_their_meeting(world: AccessOrg, superuser_engine: Engine) -> None:
    doomed = world.meeting()
    ids = seed_segments(world.engine, world.org_id, doomed, LINES)
    with transaction(world.engine, org_id=world.org_id) as tx:
        mom.seed_version(tx, world.org_id, doomed, body(ids))

    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(delete(Meeting).where(Meeting.id == doomed))

    with superuser_engine.connect() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM mom_documents WHERE meeting_id = :m"), {"m": doomed}
        ).scalar_one()
    assert left == 0


# ─── titles ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "stored", "searched"),
    [
        ("  Budget review — कल  ", "Budget review — कल", fold("Budget review कल")),
        ("Weekly sync", "Weekly sync", "vikly sync"),
        ("   ", None, None),
        (None, None, None),
    ],
)
def test_set_title_writes_the_title_and_its_fold(
    world: AccessOrg, title: str | None, stored: str | None, searched: str | None
) -> None:
    meeting_id = world.meeting(title="Old title")
    with transaction(world.engine, org_id=world.org_id) as tx:
        meeting = tx.get(Meeting, meeting_id)
        assert meeting is not None and meeting.title_search == "old title"
        set_title(tx, meeting, title)
    with transaction(world.engine, org_id=world.org_id) as tx:
        meeting = tx.get(Meeting, meeting_id)
        assert meeting is not None
        assert (meeting.title, meeting.title_search) == (stored, searched)
        if stored is not None:
            assert meeting.title_search == fold(meeting.title)
