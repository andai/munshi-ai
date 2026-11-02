"""The one write path for minutes versions (MOM-02, MOM-04): evidence, owner and date rules in
`build_document` (no database), and `write_version`'s versions, conflicts and org isolation on a
disposable Postgres."""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, select, text

from andai_core.db.models import Meeting, MomDocument
from andai_core.db.session import transaction
from andai_core.meetings import mom
from andai_core.meetings.mom import MomError
from andai_core.meetings.mom_writer import (
    VersionConflict,
    build_document,
    local_date,
    write_version,
)
from andai_core.minutes.generate import Draft
from conftest import bootstrap_org, new_user, seed_meeting, seed_segments

PROFILE = uuid.UUID("11111111-2222-5333-8444-555555555555")
MEETING_DATE = date(2026, 11, 2)  # Monday
ATTENDEES = [{"name": "Ravi Kumar", "source": "invite"}, {"name": "Asha", "source": "speaker"}]
IDS = ["seg_a", "seg_b", "seg_c"]


def draft(**overrides) -> Draft:
    body = {
        "summary": "  Budget review hua.  ",
        "decisions": [
            {"text": "Quote approve hua", "evidence": ["seg_a", "seg_invented"]},
            {"text": "Invented decision", "evidence": ["seg_nope"]},
        ],
        "actionItems": [
            {
                "text": "Final sheet bhejni hai",
                "owner": "Ravi",
                "ownerConfidence": 0.9,
                "dueText": "kal tak",
                "due": "2030-01-01",
                "evidence": ["seg_b"],
            },
            {
                "text": "Vendor ko call karna",
                "owner": "Ravi",
                "ownerConfidence": 0.4,
                "dueText": None,
                "due": "2026-11-20",
                "evidence": ["seg_c"],
            },
            {
                "text": "Invented action",
                "owner": "Asha",
                "ownerConfidence": 0.99,
                "dueText": None,
                "due": None,
                "evidence": [],
            },
        ],
        "topics": [{"title": "Budget", "evidence": ["seg_a", "seg_a", "seg_b"]}],
        "risks": [],
    }
    body.update(overrides)
    return Draft.model_validate(body)


def build(d: Draft | None = None, **kwargs):
    params = dict(
        segment_ids=IDS,
        attendees=ATTENDEES,
        meeting_date=MEETING_DATE,
        template_id="standard",
        output_language="hinglish",
        model="test-model",
        llm_profile_id=PROFILE,
    )
    params.update(kwargs)
    return build_document(d or draft(), **params)


# ─── build_document ───────────────────────────────────────────────────────────────────────────────


def test_generated_drafts_keep_only_grounded_items_and_count_the_rest() -> None:
    doc, report = build()

    assert [d.text for d in doc.decisions] == ["Quote approve hua"]
    assert doc.decisions[0].evidence == ["seg_a"]
    assert [a.text for a in doc.action_items] == ["Final sheet bhejni hai", "Vendor ko call karna"]
    assert doc.topics[0].evidence == ["seg_a", "seg_b"]
    assert doc.risks == []
    assert report.as_dict()["sections"] == {
        "decisions": {"invalid_ids": 2, "dropped": 1},
        "actionItems": {"invalid_ids": 0, "dropped": 1},
        "topics": {"invalid_ids": 0, "dropped": 0},
        "risks": {"invalid_ids": 0, "dropped": 0},
    }
    assert (report.dropped, report.invalid_ids) == (2, 2)
    assert doc.summary == "Budget review hua."


def test_generated_owners_follow_the_confidence_and_attendee_rule() -> None:
    doc, report = build()

    assert [a.owner for a in doc.action_items] == ["Ravi Kumar", "unassigned"]
    assert (report.owners_named, report.owners_unassigned) == (1, 1)


def test_generated_dates_resolve_the_phrase_and_ignore_an_unsaid_model_date() -> None:
    doc, report = build()

    # "kal tak" against Monday 2026-11-02 wins over the model's 2030-01-01
    assert doc.action_items[0].due == date(2026, 11, 3)
    # no phrase was said: the model's date is not kept
    assert doc.action_items[1].due is None
    assert (report.dates_resolved, report.dates_from_model) == (1, 0)


def test_a_model_date_is_kept_only_for_a_said_phrase_the_rules_do_not_cover() -> None:
    item = {
        "text": "Launch", "owner": None, "ownerConfidence": None,
        "dueText": "Diwali ke baad", "due": "2026-11-12", "evidence": ["seg_a"],
    }  # fmt: skip
    doc, report = build(draft(actionItems=[item]))
    assert doc.action_items[0].due == date(2026, 11, 12)
    assert report.dates_from_model == 1
    far = {**item, "due": "2029-01-01"}
    doc, _ = build(draft(actionItems=[far]))
    assert doc.action_items[0].due is None


def test_andai_fills_ids_profile_model_and_time() -> None:
    at = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    doc, _ = build(generated_at=at)

    assert doc.llm_profile_id == PROFILE and doc.model == "test-model"
    assert doc.generated_at == at and doc.edited_by is None
    assert (doc.template_id, doc.output_language) == ("standard", "hinglish")
    ids = [a.id for a in doc.action_items]
    assert len(set(ids)) == len(ids) and all(isinstance(i, uuid.UUID) for i in ids)


def test_human_edits_are_strict_and_keep_what_the_editor_typed() -> None:
    editor = uuid.uuid4()
    edit = draft(
        decisions=[{"text": "Quote approve hua", "evidence": ["seg_a"]}],
        actionItems=[
            {
                "text": "Sheet bhejo",
                "owner": "Someone New",
                "ownerConfidence": None,
                "dueText": None,
                "due": "2026-12-01",
                "evidence": ["seg_b"],
            },
            {"text": "Call karo", "owner": "", "evidence": ["seg_c"]},
        ],  # fmt: skip
    )

    doc, report = build(edit, strict=True, edited_by=editor)

    assert [a.owner for a in doc.action_items] == ["Someone New", "unassigned"]
    assert doc.action_items[0].due == date(2026, 12, 1)
    assert doc.edited_by == editor
    assert report.dropped == 0


@pytest.mark.parametrize(
    ("section", "bad", "where"),
    [
        ("decisions", [{"text": "x", "evidence": ["seg_nope"]}], "decisions[0]"),
        (
            "topics",
            [{"title": "ok", "evidence": ["seg_a"]}, {"title": "y", "evidence": []}],
            "topics[1]",
        ),
        ("risks", [{"text": "", "evidence": ["seg_a"]}], "risks[0]"),
        (
            "actionItems",
            [{"text": "x", "due": "next Friday", "evidence": ["seg_a"]}],
            "actionItems[0]",
        ),
    ],
)
def test_strict_mode_refuses_an_ungrounded_or_malformed_item_by_name(section, bad, where) -> None:
    edit = draft(
        **{**{"decisions": [], "actionItems": [], "topics": [], "risks": []}, section: bad}
    )
    with pytest.raises(MomError, match=rf"^{re.escape(where)}"):
        build(edit, strict=True)


def test_unknown_template_or_language_is_refused() -> None:
    with pytest.raises(MomError):
        build(template_id="nope")
    with pytest.raises(MomError):
        build(output_language="fr")


def test_empty_sections_stay_empty_lists() -> None:
    doc, _ = build(draft(decisions=[], actionItems=[], topics=[], risks=[]))
    body = doc.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert (body["decisions"], body["actionItems"], body["topics"], body["risks"]) == (
        [],
        [],
        [],
        [],
    )


# ─── write_version (Postgres) ─────────────────────────────────────────────────────────────────────


class World:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.user = new_user(engine)
        self.org = bootstrap_org(engine, self.user)

    def meeting(
        self,
        lines=(
            "Quote approve karte hain",
            ("Ravi Kumar", "Main kal tak bhej dunga"),
            "Vendor call",
        ),
    ) -> tuple[uuid.UUID, list[str]]:
        # 20:00 UTC on 1 November is 01:30 on 2 November in Asia/Kolkata
        meeting_id = seed_meeting(
            self.engine,
            self.org,
            self.user,
            None,
            started_at=datetime(2026, 11, 1, 20, 0, tzinfo=UTC),
        )
        ids = seed_segments(self.engine, self.org, meeting_id, list(lines))
        with transaction(self.engine, org_id=self.org) as tx:
            tx.get(Meeting, meeting_id).attendees = ATTENDEES  # type: ignore[union-attr]
        return meeting_id, ids

    def write(self, meeting_id, d: Draft, **kwargs):
        params = dict(
            template_id="standard", output_language="hinglish", model="m1", llm_profile_id=PROFILE
        )
        params.update(kwargs)
        with transaction(self.engine, org_id=params.pop("org", self.org)) as tx:
            return write_version(tx, meeting_id, d, **params)

    def versions(self, superuser: Engine, meeting_id) -> list[int]:
        with superuser.connect() as conn:
            return list(
                conn.execute(
                    text(
                        "SELECT version FROM mom_documents WHERE meeting_id = :m ORDER BY version"
                    ),
                    {"m": meeting_id},
                ).scalars()
            )


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> World:
    return World(app_engine)


def grounded(ids: list[str], **overrides) -> Draft:
    return draft(
        decisions=[{"text": "Quote approve hua", "evidence": [ids[0], "seg_invented"]}],
        actionItems=[
            {
                "text": "Sheet bhejni hai",
                "owner": "Ravi",
                "ownerConfidence": 0.8,
                "dueText": "kal tak",
                "due": None,
                "evidence": [ids[1]],
            },
            {"text": "Made up", "owner": None, "evidence": ["seg_x"]},
        ],  # fmt: skip
        topics=[{"title": "Budget", "evidence": [ids[0], ids[2]]}],
        risks=[],
        **overrides,
    )


def test_a_generated_version_is_grounded_and_stored(world: World, superuser_engine: Engine) -> None:
    meeting_id, ids = world.meeting()

    doc, report = world.write(meeting_id, grounded(ids))

    assert (doc.version, doc.meeting_id) == (1, meeting_id)
    assert report.sections["decisions"].invalid_ids == 1
    assert report.sections["actionItems"].dropped == 1
    with transaction(world.engine, org_id=world.org) as tx:
        row = mom.latest(tx, meeting_id)
        assert row is not None
        body = row.body
    assert body["version"] == 1 and body["meetingId"] == str(meeting_id)
    assert body["llmProfileId"] == str(PROFILE) and body["model"] == "m1"
    assert body["decisions"] == [{"text": "Quote approve hua", "evidence": [ids[0]]}]
    assert [a["owner"] for a in body["actionItems"]] == ["Ravi Kumar"]
    assert body["actionItems"][0]["due"] == "2026-11-03"  # kal, from the meeting's local date
    assert body["risks"] == []
    assert "editedBy" not in body
    assert (row.template_id, row.output_language) == ("standard", "hinglish")
    assert row.search_text == mom.search_text(body)


def test_versions_increment_and_edits_carry_the_editor(
    world: World, superuser_engine: Engine
) -> None:
    meeting_id, ids = world.meeting()
    world.write(meeting_id, grounded(ids))
    editor = uuid.uuid4()
    edit = draft(
        decisions=[{"text": "Edited", "evidence": [ids[0]]}], actionItems=[], topics=[], risks=[]
    )

    doc, _ = world.write(meeting_id, edit, strict=True, edited_by=editor, base_version=1)

    assert doc.version == 2 and doc.edited_by == editor
    assert world.versions(superuser_engine, meeting_id) == [1, 2]
    with transaction(world.engine, org_id=world.org) as tx:
        assert mom.latest(tx, meeting_id).body["editedBy"] == str(editor)  # type: ignore[union-attr]


def test_a_strict_edit_with_bad_evidence_writes_nothing(
    world: World, superuser_engine: Engine
) -> None:
    meeting_id, ids = world.meeting()
    bad = draft(
        decisions=[{"text": "x", "evidence": ["seg_invented"]}], actionItems=[], topics=[], risks=[]
    )

    with pytest.raises(MomError, match=r"^decisions\[0\]"):
        world.write(meeting_id, bad, strict=True)

    assert world.versions(superuser_engine, meeting_id) == []


def test_a_stale_base_version_is_a_conflict(world: World, superuser_engine: Engine) -> None:
    meeting_id, ids = world.meeting()
    world.write(meeting_id, grounded(ids))
    world.write(meeting_id, grounded(ids))

    with pytest.raises(VersionConflict):
        world.write(meeting_id, grounded(ids), strict=False, base_version=1)

    assert world.versions(superuser_engine, meeting_id) == [1, 2]


def test_a_concurrent_insert_of_the_same_version_is_retried_once(
    world: World, superuser_engine: Engine
) -> None:
    meeting_id, ids = world.meeting()
    world.write(meeting_id, grounded(ids))
    outcome: dict = {}
    other_started = threading.Event()

    def other() -> None:
        other_started.set()
        try:
            outcome["doc"] = world.write(meeting_id, grounded(ids))[0]
        except Exception as exc:  # noqa: BLE001 — reported to the test
            outcome["error"] = exc

    with transaction(world.engine, org_id=world.org) as tx:
        first, _ = write_version(
            tx, meeting_id, grounded(ids), template_id="standard", output_language="hinglish",
            model="m1", llm_profile_id=PROFILE,
        )  # fmt: skip
        assert first.version == 2
        thread = threading.Thread(target=other)
        thread.start()
        other_started.wait(5)
        time.sleep(0.5)  # the other writer reads max = 1 and waits on version 2's row
        assert thread.is_alive()
    thread.join(15)

    assert "error" not in outcome, outcome.get("error")
    assert outcome["doc"].version == 3
    assert world.versions(superuser_engine, meeting_id) == [1, 2, 3]


def test_a_second_collision_is_a_conflict(world: World, monkeypatch) -> None:
    meeting_id, ids = world.meeting()
    world.write(meeting_id, grounded(ids))
    from andai_core.meetings import mom_writer

    monkeypatch.setattr(mom_writer, "next_version", lambda tx, m: 1)

    with pytest.raises(VersionConflict):
        world.write(meeting_id, grounded(ids))


def test_another_orgs_meeting_is_no_such_meeting(
    world: World, app_engine: Engine, superuser_engine: Engine
) -> None:
    meeting_id, ids = world.meeting()
    stranger = new_user(app_engine)
    other_org = bootstrap_org(app_engine, stranger)

    with pytest.raises(MomError, match="no such meeting"):
        world.write(meeting_id, grounded(ids), org=other_org)

    assert world.versions(superuser_engine, meeting_id) == []


def test_the_meeting_date_is_local(world: World) -> None:
    meeting_id, _ = world.meeting()
    with transaction(world.engine, org_id=world.org) as tx:
        meeting = tx.get(Meeting, meeting_id)
        assert meeting is not None
        assert local_date(meeting) == date(2026, 11, 2)
        assert local_date(meeting, "UTC") == date(2026, 11, 1)


def test_seed_version_still_stores_the_next_version(world: World) -> None:
    from conftest import mom_body

    meeting_id, ids = world.meeting()
    with transaction(world.engine, org_id=world.org) as tx:
        first = mom.seed_version(tx, world.org, meeting_id, mom_body(ids))
        second = mom.seed_version(tx, world.org, meeting_id, mom_body(ids))
    assert (first.version, second.version) == (1, 2)
    with transaction(world.engine, org_id=world.org) as tx:
        assert (
            tx.scalar(
                select(MomDocument.body).where(
                    MomDocument.meeting_id == meeting_id, MomDocument.version == 2
                )
            )["version"]
            == 2
        )
