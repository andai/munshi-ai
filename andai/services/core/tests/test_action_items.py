"""`GET /api/v1/action-items/unassigned`: the action items nobody owns in the latest minutes of the
meetings the caller may read, keyset-paginated, and fast at 1,000 meetings with minutes.

The access column of the nine-caller matrix (`conftest.CALLERS`) is checked here per caller: the
items are exactly those of the meetings that caller may read.
"""

from __future__ import annotations

import json
import math
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, text
from sqlalchemy.dialects import postgresql

from andai_core.db.models import Meeting, MeetingShare, MomDocument
from andai_core.db.session import transaction
from andai_core.meetings import action_items, mom
from conftest import (
    CALLERS,
    READERS,
    AccessOrg,
    access_org,
    make_harness,
    mom_body,
    new_member,
    new_team,
    seed_meeting,
    seed_segments,
    sign_in,
)

PATH = "/api/v1/action-items/unassigned"
ITEM_FIELDS = {
    "meeting_id",
    "title",
    "platform",
    "started_at",
    "created_at",
    "team",
    "item_id",
    "text",
    "due",
    "evidence_first_id",
}
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _items(owners: list[str], evidence: list[str], label: str) -> list[dict]:
    return [
        {
            "id": str(uuid.uuid4()),
            "text": f"{label} item {n}",
            "owner": owner,
            "evidence": [evidence[n % len(evidence)]],
        }
        for n, owner in enumerate(owners)
    ]


def minuted(
    engine: Engine, org_id: uuid.UUID, meeting_id: uuid.UUID, owners: list[str], label: str = ""
) -> list[dict]:
    """Stored segments and one more minutes version whose action items have `owners`, in order.
    Returns the version's action items."""
    ids = seed_segments(engine, org_id, meeting_id, ["kal tak", "theek hai", "vendor ko bolo"])
    items = _items(owners, ids, label or meeting_id.hex[:6])
    with transaction(engine, org_id=org_id) as tx:
        mom.seed_version(tx, org_id, meeting_id, mom_body(ids, actionItems=items))
    return items


def unassigned_of(items: list[dict]) -> list[str]:
    return [i["id"] for i in items if i["owner"].lower() == "unassigned"]


def all_pages(client, **params) -> list[dict]:
    seen, cursor = [], None
    for _ in range(200):
        response = client.get(PATH, params={**params, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        body = response.json()
        seen += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("the pages never ended")


def keys(items: list[dict]) -> list[tuple[str, str]]:
    return [(i["meeting_id"], i["item_id"]) for i in items]


# ─── who sees what ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> AccessOrg:
    """The access org with three meetings with minutes, each read by a different set of callers:

    * `shared` — the requester's team meeting shared with the user and team grantees (READERS);
    * `private` — the requester's team meeting, not shared (requester, team lead, admins);
    * `own` — the unrelated member's own meeting without a team (them, admins).

    Owners are spelt as the model and editors write them; only `unassigned` in any case counts.
    """
    w = access_org(app_engine)
    started = {"shared": T0, "private": T0 - timedelta(hours=1), "own": T0 - timedelta(hours=2)}
    w.made = {
        "shared": w.meeting(started_at=started["shared"], title="Budget sync"),
        "private": w.meeting(shares=False, started_at=started["private"]),
        "own": seed_meeting(
            app_engine, w.org_id, w.people["unrelated"].user_id, None,
            started_at=started["own"],
        ),
    }  # fmt: skip
    owners = {
        "shared": ["Ravi", "unassigned", "Unassigned", "UNASSIGNED"],
        "private": ["unassigned", "Asha"],
        "own": ["Ravi", "unassigned"],
    }
    w.items = {
        name: minuted(app_engine, w.org_id, meeting_id, owners[name], name)
        for name, meeting_id in w.made.items()
    }
    return w


READERS_OF = {
    "shared": READERS,
    "private": frozenset({"requester", "lead", "admin", "owner"}),
    "own": frozenset({"unrelated", "admin", "owner"}),
}


@pytest.mark.parametrize("caller", CALLERS)
def test_each_caller_gets_the_unassigned_items_of_exactly_the_meetings_they_may_read(
    world: AccessOrg, caller: str
) -> None:
    expected = [
        (str(world.made[name]), item_id)
        for name in ("shared", "private", "own")  # newest meeting first
        if caller in READERS_OF[name]
        for item_id in unassigned_of(world.items[name])
    ]

    listed = all_pages(world.client(caller))

    assert keys(listed) == expected


def test_the_owner_is_matched_whatever_its_case(world: AccessOrg) -> None:
    texts = [i["text"] for i in all_pages(world.client("requester"))]

    assert texts[:3] == ["shared item 1", "shared item 2", "shared item 3"]
    assert "shared item 0" not in texts, "Ravi owns it"


def test_an_item_carries_what_the_section_shows(world: AccessOrg) -> None:
    listed = all_pages(world.client("user_grantee"))

    first = listed[0]
    source = world.items["shared"][1]
    assert set(first) == ITEM_FIELDS
    assert first["meeting_id"] == str(world.made["shared"])
    assert first["title"] == "Budget sync"
    assert (first["platform"], first["started_at"]) == ("zoom", T0.isoformat())
    assert first["created_at"]
    assert first["team"] == {"id": str(world.team), "name": first["team"]["name"]}
    assert first["team"]["name"].startswith("Team ")
    assert (first["item_id"], first["text"]) == (source["id"], source["text"])
    assert first["due"] is None
    assert first["evidence_first_id"] == source["evidence"][0]


def test_a_due_date_and_a_meeting_without_title_or_team_come_through(app_engine: Engine) -> None:
    owner = new_member(app_engine)
    meeting_id = seed_meeting(app_engine, owner.org_id, owner.user_id, None)
    ids = seed_segments(app_engine, owner.org_id, meeting_id, ["kal tak"])
    item = {"id": str(uuid.uuid4()), "text": "Call the vendor", "owner": "unassigned",
            "due": "2026-11-06", "evidence": ids}  # fmt: skip
    with transaction(app_engine, org_id=owner.org_id) as tx:
        mom.seed_version(tx, owner.org_id, meeting_id, mom_body(ids, actionItems=[item]))

    [listed] = sign_in(make_harness(app_engine), owner).get(PATH).json()["items"]

    assert (listed["title"], listed["team"], listed["started_at"]) == (None, None, None)
    assert listed["due"] == "2026-11-06"
    assert listed["evidence_first_id"] == ids[0]


# ─── the latest version only ──────────────────────────────────────────────────────────────────────


def _edit(evidence: list[str], owner: str) -> dict:
    return {
        "summary": "Budget sheet Friday tak.",
        "decisions": [],
        "actionItems": [
            {"text": "Budget sheet bhejna", "owner": "Ravi", "evidence": evidence[:1]},
            {"text": "Vendor se baat", "owner": owner, "evidence": evidence[1:2]},
        ],
        "topics": [],
        "risks": [],
    }


def test_an_edit_that_names_an_owner_takes_the_item_off_the_list(world: AccessOrg) -> None:
    meeting_id = world.meeting(started_at=T0 - timedelta(days=30))
    ids = seed_segments(world.engine, world.org_id, meeting_id, ["kal tak", "vendor ko bolo"])
    requester = world.client("requester")
    path = f"/api/v1/meetings/{meeting_id}/mom"
    with transaction(world.engine, org_id=world.org_id) as tx:
        mom.seed_version(tx, world.org_id, meeting_id, mom_body(ids))  # one unassigned item

    before = [i for i in all_pages(requester) if i["meeting_id"] == str(meeting_id)]
    named = requester.put(path, json=_edit(ids, "Asha"))
    after_naming = [i for i in all_pages(requester) if i["meeting_id"] == str(meeting_id)]
    cleared = requester.put(path, json=_edit(ids, ""))
    after_clearing = [i for i in all_pages(requester) if i["meeting_id"] == str(meeting_id)]

    assert [i["text"] for i in before] == ["Vendor se baat karni hai"]
    assert named.status_code == cleared.status_code == 200
    assert after_naming == [], "version 1's unassigned item no longer counts"
    assert [i["text"] for i in after_clearing] == ["Vendor se baat"]


def test_a_deleted_meeting_leaves_the_list(world: AccessOrg) -> None:
    meeting_id = world.meeting(started_at=T0 - timedelta(days=60))
    minuted(world.engine, world.org_id, meeting_id, ["unassigned"])
    grantee = world.client("team_grantee")
    assert str(meeting_id) in {i["meeting_id"] for i in all_pages(grantee)}

    deleted = world.client("requester").delete(f"/api/v1/meetings/{meeting_id}")

    assert deleted.status_code == 202, deleted.text
    for caller in ("team_grantee", "requester", "owner"):
        assert str(meeting_id) not in {i["meeting_id"] for i in all_pages(world.client(caller))}


def test_a_revoked_share_takes_the_items_with_it(world: AccessOrg) -> None:
    meeting_id = world.meeting(started_at=T0 - timedelta(days=90))
    minuted(world.engine, world.org_id, meeting_id, ["unassigned"])
    grantee = world.client("user_grantee")
    requester = world.client("requester")
    assert str(meeting_id) in {i["meeting_id"] for i in all_pages(grantee)}
    [share] = [
        s
        for s in requester.get(f"/api/v1/meetings/{meeting_id}/shares").json()["shares"]
        if s["grantee_type"] == "user"
    ]

    revoked = requester.delete(f"/api/v1/meetings/{meeting_id}/shares/{share['id']}")

    assert revoked.status_code == 204
    assert str(meeting_id) not in {i["meeting_id"] for i in all_pages(grantee)}


def test_nobody_has_none_when_nothing_is_unassigned(app_engine: Engine) -> None:
    owner = new_member(app_engine)
    meeting_id = seed_meeting(app_engine, owner.org_id, owner.user_id, None)
    minuted(app_engine, owner.org_id, meeting_id, ["Ravi", "Asha"])
    seed_meeting(app_engine, owner.org_id, owner.user_id, None)  # no minutes at all

    body = sign_in(make_harness(app_engine), owner).get(PATH).json()

    assert body == {"items": [], "next_cursor": None}


# ─── pages ────────────────────────────────────────────────────────────────────────────────────────


def test_pages_follow_the_list_order_without_repeats_or_gaps(app_engine: Engine) -> None:
    owner = new_member(app_engine)
    client = sign_in(make_harness(app_engine), owner)
    times = [T0 - timedelta(hours=h) for h in (0, 1, 2, 2, 2, 3, 4, 5, 6, 7, 8, 9)]  # ties at 2 h
    made = [
        seed_meeting(app_engine, owner.org_id, owner.user_id, None, started_at=t) for t in times
    ]
    owners = ["unassigned", "Ravi", "unassigned", "unassigned"]  # 3 per meeting, 36 in all
    items = {m: minuted(app_engine, owner.org_id, m, owners) for m in made}
    expected = [
        (str(m), item_id)
        for _, m in sorted(zip(times, made, strict=True), key=lambda p: (p[0], p[1]), reverse=True)
        for item_id in unassigned_of(items[m])
    ]

    default = client.get(PATH).json()
    widest = client.get(PATH, params={"limit": 50}).json()
    paged = all_pages(client, limit=5)

    assert len(default["items"]) == 25 and default["next_cursor"]
    assert keys(widest["items"]) == expected and widest["next_cursor"] is None
    assert keys(paged) == expected, "a page boundary inside a meeting neither repeats nor skips"
    assert len(set(keys(paged))) == 36
    for limit in (0, 51, -1):
        assert client.get(PATH, params={"limit": limit}).status_code == 422


@pytest.mark.parametrize(
    "cursor",
    ["garbage", "e30", "eyJ0IjoieCJ9", "", "a" * 600],
    ids=["not base64", "empty object", "bad time", "empty", "oversized"],
)
def test_a_bad_cursor_is_422(world: AccessOrg, cursor: str) -> None:
    response = world.client("admin").get(PATH, params={"cursor": cursor})

    assert (response.status_code, response.json()["code"]) == (422, "bad_cursor")


def test_a_cursor_with_a_bad_position_is_422(world: AccessOrg) -> None:
    for ordinal in (0, -1, "1", True):
        raw = json.dumps({"t": T0.isoformat(), "i": str(uuid.uuid4()), "n": ordinal})
        cursor = action_items.base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        response = world.client("admin").get(PATH, params={"cursor": cursor})
        assert (response.status_code, response.json()["code"]) == (422, "bad_cursor"), ordinal


def test_anonymous_is_401(world: AccessOrg) -> None:
    assert world.harness.client().get(PATH).status_code == 401


# ─── 1,000 meetings with minutes ──────────────────────────────────────────────────────────────────

CALLS = 20
P95_BUDGET_S = 0.300


def _body(meeting_id: uuid.UUID, version: int, owners: list[str]) -> dict:
    doc = mom_body(["seg_x"], actionItems=_items(owners, ["seg_x"], f"m{meeting_id.hex[:6]}"))
    doc.update(meetingId=str(meeting_id), version=version)
    return doc


def _seed_thousand(w: AccessOrg) -> dict[str, int]:
    """1,000 meetings, each with minutes; a tenth have two versions, the newer naming every owner.
    Returns how many unassigned items each of lead / unrelated / admin should see."""
    third = new_team(w.engine, w.org_id, {w.people["requester"].user_id: "member"})
    teams = (w.team, w.shared_team, third)
    requesters = [w.people[c].user_id for c in ("requester", "lead", "team_member", "unrelated")]
    meetings, shares, docs = [], [], []
    for n in range(1000):
        meeting_id = uuid.uuid4()
        started = T0 - timedelta(hours=n * 7)
        meetings.append(
            {
                "id": meeting_id,
                "org_id": w.org_id,
                "team_id": teams[n % 3],
                "requested_by": requesters[n % len(requesters)],
                "platform": ("google_meet", "teams", "zoom")[n % 3],
                "join_url": f"https://zoom.us/j/{n}",
                "join_url_key": f"zoom.us/j/{meeting_id.hex}",
                "status": "done",
                "started_at": started,
                "ended_at": started + timedelta(minutes=30),
                "title": f"Meeting {n}",
            }
        )
        if n % 20 == 3:
            grantee = ("user", w.people["unrelated"].user_id) if n % 40 == 3 else ("team", third)
            shares.append(
                {
                    "org_id": w.org_id,
                    "meeting_id": meeting_id,
                    "grantee_type": grantee[0],
                    "grantee_id": grantee[1],
                    "granted_by": meetings[-1]["requested_by"],
                }
            )
        versions = [["Ravi", "unassigned", "Asha", "Unassigned", "Neha", "Kiran"]]
        if n % 10 == 0:
            versions.append(["Ravi", "Asha", "Asha", "Neha", "Neha", "Kiran"])
        for number, owners in enumerate(versions, start=1):
            docs.append(
                {
                    "meeting_id": meeting_id,
                    "version": number,
                    "org_id": w.org_id,
                    "template_id": "standard",
                    "output_language": "hinglish",
                    "body": _body(meeting_id, number, owners),
                    "search_text": "",
                }
            )
    with transaction(w.engine, org_id=w.org_id) as tx:
        tx.execute(insert(Meeting.__table__), meetings)
        tx.execute(insert(MeetingShare.__table__), shares)
        tx.execute(insert(MomDocument.__table__), docs)
    assert (len(shares), len(docs)) == (50, 1100)

    shared_with_unrelated = {s["meeting_id"] for s in shares if s["grantee_type"] == "user"}

    def reads(caller: str, row: dict) -> bool:
        if caller == "admin":
            return True
        person = w.people[caller].user_id
        if caller == "lead":
            return row["requested_by"] == person or row["team_id"] == w.team
        return row["requested_by"] == person or row["id"] in shared_with_unrelated

    current = {row["id"]: 0 if n % 10 == 0 else 2 for n, row in enumerate(meetings)}
    return {
        caller: sum(current[row["id"]] for row in meetings if reads(caller, row))
        for caller in ("lead", "unrelated", "admin")
    }


def _p95(samples: list[float]) -> float:
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


def _plan(engine: Engine, w: AccessOrg, caller: str) -> dict:
    actor = w.actor(caller)
    statement = action_items.query(actor, action_items.DEFAULT_LIMIT + 1, None)
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
    )
    with transaction(engine, org_id=actor.org_id, user_id=actor.user_id) as tx:
        raw = tx.connection().exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + str(compiled), compiled.params
        ).scalar_one()
    return (raw if isinstance(raw, list) else json.loads(raw))[0]


def _reads(node: dict, relation: str) -> set[str]:
    """How the plan reads `relation`: its index names, or `Seq Scan`."""
    found = set()
    if node.get("Relation Name") == relation:
        found.add(node.get("Index Name") or node["Node Type"])
    for child in node.get("Plans", []):
        found |= _reads(child, relation)
    return found


def test_a_thousand_meetings_with_minutes_list_within_300_ms_at_p95(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    w = access_org(app_engine)
    counts = _seed_thousand(w)
    with superuser_engine.connect() as conn:
        conn.execute(text("ANALYZE meetings, meeting_shares, team_memberships, mom_documents"))
        conn.commit()

    timings = {}
    for caller, count in counts.items():
        client = w.client(caller)
        assert len(all_pages(client, limit=50)) == count, caller
        samples = []
        for _ in range(CALLS):
            start = time.perf_counter()
            response = client.get(PATH)
            samples.append(time.perf_counter() - start)
            assert response.status_code == 200
        timings[caller] = samples

    plans = {caller: _plan(app_engine, w, caller) for caller in counts}
    report = {
        caller: {
            "items": counts[caller],
            "p95_ms": round(_p95(s) * 1000, 1),
            "median_ms": round(statistics.median(s) * 1000, 1),
            "max_ms": round(max(s) * 1000, 1),
            "execution_ms": plans[caller]["Execution Time"],
            "meetings_read_by": sorted(_reads(plans[caller]["Plan"], "meetings")),
            "minutes_read_by": sorted(_reads(plans[caller]["Plan"], "mom_documents")),
        }
        for caller, s in timings.items()
    }
    print("\nUNASSIGNED TIMING", json.dumps(report, indent=1))

    for caller, samples in timings.items():
        assert _p95(samples) <= P95_BUDGET_S, (caller, report[caller])
    for caller in counts:
        # The latest version is one step down the primary key, never a scan of every version.
        assert report[caller]["minutes_read_by"] == ["pk_mom_documents"], report[caller]
