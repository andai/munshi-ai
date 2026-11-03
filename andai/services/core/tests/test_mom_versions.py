"""Minutes versions (MOM-03, MOM-04): reading any version with its template's headings, the
version list, a human edit saved as the next version, and a regenerate request with another
template or output language that the minutes job picks up.

Who may (`.work/AUTH_MATRIX.md`): every reader of the meeting reads versions; the requester, a lead
of the meeting's team, admins and owners edit and regenerate (`access.can_share`); grantees and
everyone else get the 404 a meeting they may not manage answers. Versions are only ever added.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import Engine, text

from andai_core.db.session import transaction
from andai_core.llm.client import LlmClient, LlmConfig
from andai_core.meetings import mom
from andai_core.meetings.minutes_job import run_one
from andai_core.minutes import templates
from andai_core.testing import fake_llm
from conftest import CALLERS, READERS, SHARERS, AccessOrg, access_org, mom_body, seed_segments

LINES = [
    ("Asha", "कल तक deadline है"),
    ("Ravi", "theek hai, main budget sheet bhej dunga"),
    ("Asha", "vendor se baat karni hai"),
]


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> AccessOrg:
    return access_org(app_engine)


def _minuted(world: AccessOrg, versions: int = 1, **fields) -> tuple[uuid.UUID, list[str]]:
    """A finished meeting with a stored transcript and `versions` stored minutes versions."""
    meeting_id = world.meeting(**fields)
    ids = seed_segments(world.engine, world.org_id, meeting_id, LINES)
    with transaction(world.engine, org_id=world.org_id) as tx:
        for n in range(versions):
            mom.seed_version(tx, world.org_id, meeting_id, mom_body(ids, summary=f"draft {n + 1}"))
    _set(world, meeting_id, mom_state="stored")
    return meeting_id, ids


def _set(world: AccessOrg, meeting_id: uuid.UUID, **values: object) -> None:
    casts = {"mom_request": "jsonb"}
    assignments = ", ".join(
        f"{c} = CAST(:{c} AS {casts[c]})" if c in casts else f"{c} = :{c}" for c in values
    )
    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(
            text(f"UPDATE meetings SET {assignments} WHERE id = :m"), {"m": meeting_id, **values}
        )


def _row(superuser_engine: Engine, meeting_id: uuid.UUID) -> dict:
    with superuser_engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT mom_state, mom_attempts, mom_next_at, mom_request, mom_lease,"
                    " mom_lease_until, mom_error FROM meetings WHERE id = :m"
                ),
                {"m": meeting_id},
            ).mappings().one()
        )


def _audits(superuser_engine: Engine, action: str, meeting_id: uuid.UUID) -> int:
    with superuser_engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM audit_events WHERE action = :a AND target LIKE :t"),
            {"a": action, "t": f"meeting:{meeting_id}%"},
        ).scalar_one()


def _stored_versions(superuser_engine: Engine, meeting_id: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT version, template_id, output_language, body->>'summary'"
                    " FROM mom_documents WHERE meeting_id = :m ORDER BY version"
                ),
                {"m": meeting_id},
            )
        ]


def _path(meeting_id: uuid.UUID, tail: str = "") -> str:
    return f"/api/v1/meetings/{meeting_id}/mom{tail}"


def _refused(response) -> bool:
    return response.status_code == 404 and response.json()["code"] == "no_such_meeting"


def _edit(ids: list[str], **overrides) -> dict:
    """The edit body: the draft fields, as the page sends them."""
    body = {
        "summary": "Budget sheet Friday tak.",
        "decisions": [{"text": "Deadline kal hai", "evidence": ids[:1]}],
        "actionItems": [
            {
                "text": "Budget sheet bhejna",
                "owner": "Ravi",
                "due": "2026-11-06",
                "evidence": ids[1:2],
            },
            {"text": "Vendor se baat", "owner": "", "evidence": ids[2:3]},
        ],
        "topics": [{"title": "Budget", "evidence": ids[1:3]}],
        "risks": [],
    }
    body.update(overrides)
    return body


# ─── reading versions ─────────────────────────────────────────────────────────────────────────────


def test_the_latest_version_carries_its_templates_headings_and_sections(world: AccessOrg) -> None:
    meeting_id, _ = _minuted(world, versions=2)

    doc = world.client("requester").get(_path(meeting_id)).json()

    assert doc["version"] == 2 and doc["summary"] == "draft 2"
    standard = templates.get("standard")
    assert doc["sections"] == list(standard.sections)
    assert doc["headings"] == dict(standard.headings["hinglish"])
    assert doc["labels"] == {
        "noneRecorded": templates.NONE_RECORDED["hinglish"],
        "unassigned": templates.UNASSIGNED_LABEL["hinglish"],
    }


def test_a_hindi_formal_version_has_devanagari_headings(world: AccessOrg) -> None:
    meeting_id, ids = _minuted(world, versions=0)
    with transaction(world.engine, org_id=world.org_id) as tx:
        mom.seed_version(
            tx, world.org_id, meeting_id,
            mom_body(ids, templateId="indian_formal_mom", outputLanguage="hi", summary="सारांश"),
        )  # fmt: skip

    doc = world.client("requester").get(_path(meeting_id)).json()

    formal = templates.get("indian_formal_mom")
    assert doc["sections"] == list(formal.sections)
    assert doc["headings"]["decisions"] == "लिए गए निर्णय"
    assert doc["headings"] == dict(formal.headings["hi"])
    assert doc["labels"]["noneRecorded"] == "कुछ दर्ज नहीं हुआ"


def test_any_version_can_be_opened(world: AccessOrg) -> None:
    meeting_id, _ = _minuted(world, versions=3)
    client = world.client("user_grantee")

    for n in (1, 2, 3):
        doc = client.get(_path(meeting_id), params={"version": n}).json()
        assert (doc["version"], doc["summary"]) == (n, f"draft {n}")
    missing = client.get(_path(meeting_id), params={"version": 4})
    assert missing.status_code == 404 and missing.json()["code"] == "no_such_version"
    assert client.get(_path(meeting_id), params={"version": 0}).status_code == 422


def test_the_version_list_names_template_language_and_who_and_when(
    world: AccessOrg, superuser_engine: Engine
) -> None:
    meeting_id, ids = _minuted(world, versions=2)
    editor = world.client("lead")
    saved = editor.put(_path(meeting_id), json=_edit(ids, baseVersion=2))
    assert saved.status_code == 200, saved.text

    response = world.client("team_grantee").get(_path(meeting_id, "/versions"))

    assert response.status_code == 200, response.text
    versions = response.json()["versions"]
    assert [v["version"] for v in versions] == [3, 2, 1]
    newest = versions[0]
    assert set(newest) == {
        "version", "templateId", "outputLanguage", "model", "generatedAt", "editedBy",
        "editedByName", "createdAt",
    }  # fmt: skip
    assert (newest["templateId"], newest["outputLanguage"]) == ("standard", "hinglish")
    assert newest["editedBy"] == str(world.people["lead"].user_id)
    assert newest["editedByName"] == "Test Member"
    assert versions[1]["editedBy"] is None and versions[1]["editedByName"] is None
    assert versions[1]["model"] == "test-model"
    datetime.fromisoformat(newest["generatedAt"])
    datetime.fromisoformat(newest["createdAt"])


def test_a_readable_meeting_without_minutes_lists_no_versions(world: AccessOrg) -> None:
    bare = world.meeting()

    response = world.client("requester").get(_path(bare, "/versions"))

    assert (response.status_code, response.json()) == (200, {"versions": []})


# ─── who may (nine callers) ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", CALLERS)
def test_versions_are_read_by_readers_only(world: AccessOrg, caller: str) -> None:
    meeting_id, _ = _minuted(world)
    client = world.client(caller)

    listed = client.get(_path(meeting_id, "/versions"))
    opened = client.get(_path(meeting_id), params={"version": 1})

    if caller in READERS:
        assert listed.status_code == 200 and opened.status_code == 200
    else:
        assert _refused(listed) and _refused(opened)


@pytest.mark.parametrize("caller", CALLERS)
def test_edits_are_saved_by_sharers_only(
    world: AccessOrg, superuser_engine: Engine, caller: str
) -> None:
    meeting_id, ids = _minuted(world)

    response = world.client(caller).put(_path(meeting_id), json=_edit(ids))

    if caller in SHARERS:
        assert response.status_code == 200, response.text
        doc = response.json()
        assert doc["version"] == 2
        assert doc["editedBy"] == str(world.people[caller].user_id)
        assert _audits(superuser_engine, "mom.edit", meeting_id) == 1
    else:
        assert _refused(response), response.text
        assert _audits(superuser_engine, "mom.edit", meeting_id) == 0
    assert len(_stored_versions(superuser_engine, meeting_id)) == (
        2 if caller in SHARERS else 1
    )


@pytest.mark.parametrize("caller", CALLERS)
def test_regenerates_are_requested_by_sharers_only(
    world: AccessOrg, superuser_engine: Engine, caller: str
) -> None:
    meeting_id, _ = _minuted(world)

    response = world.client(caller).post(
        _path(meeting_id, "/regenerate"),
        json={"templateId": "client_call", "outputLanguage": "en"},
    )

    row = _row(superuser_engine, meeting_id)
    if caller in SHARERS:
        assert response.status_code == 202, response.text
        assert row["mom_state"] == "pending"
        assert _audits(superuser_engine, "mom.regenerate", meeting_id) == 1
    else:
        assert _refused(response), response.text
        assert (row["mom_state"], row["mom_request"]) == ("stored", None)
        assert _audits(superuser_engine, "mom.regenerate", meeting_id) == 0


# ─── edit ─────────────────────────────────────────────────────────────────────────────────────────


def test_an_edit_is_the_next_version_and_keeps_the_old_ones(
    world: AccessOrg, superuser_engine: Engine
) -> None:
    meeting_id, ids = _minuted(world, versions=2)
    before = _stored_versions(superuser_engine, meeting_id)

    response = world.client("requester").put(_path(meeting_id), json=_edit(ids, baseVersion=2))

    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["version"] == 3
    assert doc["summary"] == "Budget sheet Friday tak."
    assert (doc["templateId"], doc["outputLanguage"]) == ("standard", "hinglish")
    assert doc["model"] == "test-model"
    assert doc["editedBy"] == str(world.people["requester"].user_id)
    assert [a["owner"] for a in doc["actionItems"]] == ["Ravi", "unassigned"]
    assert doc["actionItems"][0]["due"] == "2026-11-06"
    assert doc["headings"]["summary"] == "Summary"
    after = _stored_versions(superuser_engine, meeting_id)
    assert after[:2] == before, "older versions are unchanged"
    assert after[2] == (3, "standard", "hinglish", "Budget sheet Friday tak.")
    assert world.client("user_grantee").get(_path(meeting_id)).json()["version"] == 3


def test_a_stale_base_version_is_a_conflict(world: AccessOrg, superuser_engine: Engine) -> None:
    meeting_id, ids = _minuted(world, versions=2)

    response = world.client("admin").put(_path(meeting_id), json=_edit(ids, baseVersion=1))

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "version_conflict"
    assert len(_stored_versions(superuser_engine, meeting_id)) == 2
    assert _audits(superuser_engine, "mom.edit", meeting_id) == 0


@pytest.mark.parametrize(
    ("where", "change"),
    [
        ("actionItems[1]", lambda ids, e: e["actionItems"][1].update(evidence=["seg_made_up"])),
        ("decisions[0]", lambda ids, e: e["decisions"][0].update(evidence=[])),
        ("topics[0]", lambda ids, e: e["topics"][0].update(evidence=[*ids[:1], "seg_x"])),
        ("actionItems[0]", lambda ids, e: e["actionItems"][0].update(due="next friday")),
        ("decisions[0]", lambda ids, e: e["decisions"][0].update(text="  ")),
    ],
)
def test_an_edit_without_valid_evidence_is_refused_naming_the_item(
    world: AccessOrg, superuser_engine: Engine, where: str, change
) -> None:
    meeting_id, ids = _minuted(world)
    body = _edit(ids)
    change(ids, body)

    response = world.client("owner").put(_path(meeting_id), json=body)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_minutes"
    assert where in response.json()["detail"]
    assert len(_stored_versions(superuser_engine, meeting_id)) == 1
    assert _audits(superuser_engine, "mom.edit", meeting_id) == 0


def test_evidence_of_another_meeting_is_invented_evidence(world: AccessOrg) -> None:
    meeting_id, ids = _minuted(world)
    _, other_ids = _minuted(world)

    response = world.client("owner").put(
        _path(meeting_id), json=_edit(ids, decisions=[{"text": "x", "evidence": other_ids[:1]}])
    )

    assert response.status_code == 422 and "decisions[0]" in response.json()["detail"]


def test_an_edit_body_outside_the_draft_shape_is_422(world: AccessOrg) -> None:
    meeting_id, ids = _minuted(world)
    client = world.client("owner")

    assert client.put(_path(meeting_id), json=_edit(ids, nextSteps=[])).status_code == 422
    assert client.put(_path(meeting_id), json={"summary": "only"}).status_code == 422


def test_editing_a_meeting_without_minutes_is_404_no_minutes(world: AccessOrg) -> None:
    bare = world.meeting()
    ids = seed_segments(world.engine, world.org_id, bare, LINES)

    response = world.client("requester").put(_path(bare), json=_edit(ids))

    assert response.status_code == 404 and response.json()["code"] == "no_minutes"


# ─── regenerate ───────────────────────────────────────────────────────────────────────────────────


def test_a_regenerate_queues_the_template_and_language(
    world: AccessOrg, superuser_engine: Engine
) -> None:
    meeting_id, _ = _minuted(world)
    _set(world, meeting_id, mom_state="failed", mom_attempts=8, mom_error="llm_timeout")

    response = world.client("lead").post(
        _path(meeting_id, "/regenerate"),
        json={"templateId": "indian_formal_mom", "outputLanguage": "hi"},
    )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "id": str(meeting_id),
        "mom_state": "pending",
        "templateId": "indian_formal_mom",
        "outputLanguage": "hi",
    }
    row = _row(superuser_engine, meeting_id)
    assert (row["mom_state"], row["mom_attempts"], row["mom_error"]) == ("pending", 0, None)
    assert row["mom_next_at"] is not None
    assert row["mom_next_at"] <= datetime.now(UTC) + timedelta(seconds=1)
    request = row["mom_request"]
    assert (request["template"], request["language"]) == ("indian_formal_mom", "hi")
    assert request["requestedBy"] == str(world.people["lead"].user_id)
    datetime.fromisoformat(request["requestedAt"])
    assert len(_stored_versions(superuser_engine, meeting_id)) == 1, "nothing is replaced"


def test_a_second_regenerate_while_one_is_pending_is_409(
    world: AccessOrg, superuser_engine: Engine
) -> None:
    meeting_id, _ = _minuted(world)
    client = world.client("requester")
    body = {"templateId": "standup", "outputLanguage": "en"}
    assert client.post(_path(meeting_id, "/regenerate"), json=body).status_code == 202
    first = _row(superuser_engine, meeting_id)["mom_request"]

    again = client.post(
        _path(meeting_id, "/regenerate"), json={"templateId": "client_call", "outputLanguage": "hi"}
    )

    assert again.status_code == 409 and again.json()["code"] == "minutes_pending"
    assert _row(superuser_engine, meeting_id)["mom_request"] == first
    assert _audits(superuser_engine, "mom.regenerate", meeting_id) == 1


def test_a_regenerate_during_a_running_generation_leaves_its_lease(
    world: AccessOrg, superuser_engine: Engine
) -> None:
    meeting_id, _ = _minuted(world)
    lease, until = uuid.uuid4(), datetime.now(UTC) + timedelta(minutes=15)
    _set(world, meeting_id, mom_state="pending", mom_lease=lease, mom_lease_until=until)

    response = world.client("requester").post(
        _path(meeting_id, "/regenerate"), json={"templateId": "standard", "outputLanguage": "en"}
    )

    assert response.status_code == 202, response.text
    row = _row(superuser_engine, meeting_id)
    assert (row["mom_lease"], row["mom_lease_until"]) == (lease, until)
    assert row["mom_request"]["language"] == "en"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"templateId": "minutes_of_doom", "outputLanguage": "en"}, "unknown_template"),
        ({"templateId": "standard", "outputLanguage": "fr"}, "unknown_language"),
    ],
)
def test_an_unknown_template_or_language_is_422(
    world: AccessOrg, superuser_engine: Engine, body: dict, code: str
) -> None:
    meeting_id, _ = _minuted(world)

    response = world.client("owner").post(_path(meeting_id, "/regenerate"), json=body)

    assert response.status_code == 422 and response.json()["code"] == code
    assert _row(superuser_engine, meeting_id)["mom_state"] == "stored"


def test_a_regenerate_body_must_name_both(world: AccessOrg) -> None:
    meeting_id, _ = _minuted(world)
    client = world.client("owner")

    for body in ({"templateId": "standard"}, {"outputLanguage": "hi"}, {}):
        assert client.post(_path(meeting_id, "/regenerate"), json=body).status_code == 422


def test_a_regenerate_before_the_transcript_is_stored_is_409(world: AccessOrg) -> None:
    live = world.meeting(status="recording")

    response = world.client("requester").post(
        _path(live, "/regenerate"), json={"templateId": "standard", "outputLanguage": "en"}
    )

    assert response.status_code == 409 and response.json()["code"] == "transcript_not_ready"


def test_a_deleted_meeting_is_404_for_every_new_route(world: AccessOrg) -> None:
    meeting_id, ids = _minuted(world)
    client = world.client("requester")
    assert client.delete(f"/api/v1/meetings/{meeting_id}").status_code == 202

    assert _refused(client.get(_path(meeting_id, "/versions")))
    assert _refused(client.get(_path(meeting_id), params={"version": 1}))
    assert _refused(client.put(_path(meeting_id), json=_edit(ids)))
    assert _refused(
        client.post(
            _path(meeting_id, "/regenerate"),
            json={"templateId": "standard", "outputLanguage": "en"},
        )
    )


# ─── the request reaches the minutes job ──────────────────────────────────────────────────────────


def test_the_minutes_job_writes_the_regenerated_version(fresh_app_engine: Engine) -> None:
    """On a database of its own (the claim sees every org): regenerate → claim → run → version 2
    in the requested template and language, version 1 kept."""
    world = access_org(fresh_app_engine)
    meeting_id, _ = _minuted(world)

    response = world.client("requester").post(
        _path(meeting_id, "/regenerate"),
        json={"templateId": "indian_formal_mom", "outputLanguage": "hi"},
    )
    assert response.status_code == 202, response.text
    with transaction(fresh_app_engine) as tx:
        claimed = {
            r.meeting_id: r.lease
            for r in tx.execute(
                text("SELECT org_id, meeting_id, lease FROM andai_claim_mom(:n)"), {"n": 10}
            )
        }
    assert set(claimed) == {meeting_id}
    llm = LlmClient(
        LlmConfig(base_url="http://fake-llm.test", api_key=None, model="fake-model"),
        transport=httpx.ASGITransport(app=fake_llm.app),
    )

    result = asyncio.run(
        run_one(fresh_app_engine, llm, world.org_id, meeting_id, claimed[meeting_id])
    )

    assert (result.state, result.version, result.first) == ("stored", 2, False)
    doc = world.client("user_grantee").get(_path(meeting_id)).json()
    assert (doc["version"], doc["templateId"], doc["outputLanguage"]) == (
        2, "indian_formal_mom", "hi",
    )  # fmt: skip
    assert doc["headings"]["decisions"] == "लिए गए निर्णय"
    versions = world.client("requester").get(_path(meeting_id, "/versions")).json()["versions"]
    assert [v["version"] for v in versions] == [2, 1]
