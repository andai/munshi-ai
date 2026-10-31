"""`GET /api/v1/meetings`: the meetings the caller may read, as Live / Upcoming / Past sections,
filtered, keyset-paginated, and fast at 1,000 meetings.

The access column of the nine-caller matrix (`conftest.CALLERS`) is checked here; the detail,
transcript, live and stop columns are `test_meeting_access.py`, sharing is `test_shares.py`.
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

from andai_core.db.models import Meeting, MeetingShare
from andai_core.db.session import transaction
from andai_core.meetings import listing
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

PATH = "/api/v1/meetings"
ITEM_FIELDS = {
    "id",
    "title",
    "platform",
    "status",
    "failure_reason",
    "end_reason",
    "team",
    "requested_by",
    "requested_by_name",
    "started_at",
    "ended_at",
    "created_at",
    "duration_s",
    "shared",
    "has_minutes",
}
SECTIONS = {
    "live": ("dispatching", "awaiting_admission", "recording", "processing"),
    "upcoming": ("scheduled",),
    "past": ("done", "failed", "cancelled"),
}
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> AccessOrg:
    w = access_org(app_engine)
    w.meeting_id = w.meeting()
    return w


def sign_in_owner(engine: Engine, owner):
    return sign_in(make_harness(engine), owner)


def all_pages(client, **params) -> list[str]:
    seen, cursor = [], None
    for _ in range(100):
        query = {"cursor": cursor} if cursor else params
        body = client.get(PATH, params=query).json()
        [(section, page)] = body["sections"].items()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("the pages never ended")


# ─── who sees what ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", CALLERS)
def test_the_list_holds_the_meeting_only_for_its_readers(world: AccessOrg, caller: str) -> None:
    listed = all_pages(world.client(caller), section="past")

    assert (str(world.meeting_id) in listed) == (caller in READERS)


def test_a_member_lists_only_meetings_they_may_read(world: AccessOrg) -> None:
    hidden = world.meeting(shares=False)
    own = seed_meeting(world.engine, world.org_id, world.people["unrelated"].user_id, None)

    listed = all_pages(world.client("unrelated"), section="past")

    assert own in map(uuid.UUID, listed)
    assert hidden not in map(uuid.UUID, listed)
    assert str(world.meeting_id) not in listed


# ─── sections and items ───────────────────────────────────────────────────────────────────────────


def test_sections_by_status_newest_first(app_engine: Engine) -> None:
    owner = new_member(app_engine)
    made = {}
    for n, status in enumerate(s for statuses in SECTIONS.values() for s in statuses):
        made[status] = seed_meeting(
            app_engine, owner.org_id, owner.user_id, None, status=status,
            started_at=T0 + timedelta(minutes=n),
        )

    body = sign_in_owner(app_engine, owner).get(PATH).json()

    assert set(body["sections"]) == set(SECTIONS)
    for section, statuses in SECTIONS.items():
        page = body["sections"][section]
        assert [item["id"] for item in page["items"]] == [
            str(made[s]) for s in reversed(statuses)
        ], section
        assert page["next_cursor"] is None


def test_an_item_carries_what_the_dashboard_shows(world: AccessOrg) -> None:
    started = T0 - timedelta(days=400)
    meeting_id = world.meeting(
        title="Weekly sync", started_at=started, ended_at=started + timedelta(minutes=42, seconds=5)
    )

    body = world.client("user_grantee").get(PATH, params={"section": "past", "limit": 50}).json()

    [item] = [i for i in body["sections"]["past"]["items"] if i["id"] == str(meeting_id)]
    assert set(item) == ITEM_FIELDS
    assert item["title"] == "Weekly sync"
    assert item["team"]["id"] == str(world.team) and item["team"]["name"].startswith("Team ")
    assert item["requested_by"] == str(world.people["requester"].user_id)
    assert item["requested_by_name"] == "Test Member"
    assert item["duration_s"] == 42 * 60 + 5
    assert item["shared"] is True
    assert item["has_minutes"] is False
    assert (item["platform"], item["status"]) == ("zoom", "done")
    own = world.client("requester").get(PATH, params={"section": "past", "limit": 50}).json()
    [mine] = [i for i in own["sections"]["past"]["items"] if i["id"] == str(meeting_id)]
    assert mine["shared"] is False, "shared means: reaches the caller through a share"


def test_has_minutes_follows_the_minutes(world: AccessOrg) -> None:
    from andai_core.meetings import mom

    started = T0 - timedelta(days=500)
    with_minutes = world.meeting(started_at=started)
    without = world.meeting(started_at=started - timedelta(hours=1))
    ids = seed_segments(world.engine, world.org_id, with_minutes, ["kal tak", "theek hai"])
    with transaction(world.engine, org_id=world.org_id) as tx:
        mom.seed_version(tx, world.org_id, with_minutes, mom_body(ids))

    listed = {
        i["id"]: i["has_minutes"]
        for i in world.client("team_grantee")
        .get(PATH, params={"section": "past", "limit": 50})
        .json()["sections"]["past"]["items"]
    }

    assert listed[str(with_minutes)] is True
    assert listed[str(without)] is False



# ─── filters ──────────────────────────────────────────────────────────────────────────────────────


def test_filter_mine(world: AccessOrg) -> None:
    lead_own = seed_meeting(world.engine, world.org_id, world.people["lead"].user_id, world.team)

    listed = all_pages(world.client("lead"), section="past", filter="mine")
    everything = all_pages(world.client("lead"), section="past")

    assert str(lead_own) in listed
    assert str(world.meeting_id) not in listed
    assert str(world.meeting_id) in everything


def test_filter_shared(world: AccessOrg) -> None:
    listed = all_pages(world.client("team_grantee"), section="past", filter="shared")
    requester = all_pages(world.client("requester"), section="past", filter="shared")

    assert str(world.meeting_id) in listed
    assert str(world.meeting_id) not in requester, "your own meeting is not shared with you"


def test_filter_team(world: AccessOrg) -> None:
    other = seed_meeting(world.engine, world.org_id, world.people["admin"].user_id, None)
    admin = world.client("admin")

    team = all_pages(admin, section="past", filter=f"team:{world.team}")
    shared_team = all_pages(admin, section="past", filter=f"team:{world.shared_team}")

    assert str(world.meeting_id) in team and str(other) not in team
    assert shared_team == []


@pytest.mark.parametrize("value", ["everything", "team:", "team:not-a-uuid", "TEAM:x"])
def test_an_unknown_filter_is_422(world: AccessOrg, value: str) -> None:
    response = world.client("admin").get(PATH, params={"filter": value})

    assert (response.status_code, response.json()["code"]) == (422, "bad_filter")


# ─── pages ────────────────────────────────────────────────────────────────────────────────────────


def test_the_cursor_is_stable_across_inserts_and_ties(app_engine: Engine) -> None:
    owner = new_member(app_engine)
    client = sign_in_owner(app_engine, owner)
    times = [T0 - timedelta(hours=h) for h in (0, 1, 2, 2, 2, 3, 4)]  # three share a time
    made = [
        seed_meeting(app_engine, owner.org_id, owner.user_id, None, started_at=t) for t in times
    ]
    expected = [
        str(m) for _, m in sorted(zip(times, made, strict=True), key=lambda p: (p[0], p[1]),
                                  reverse=True)
    ]

    first = client.get(PATH, params={"section": "past", "limit": 3}).json()["sections"]["past"]
    for _ in range(2):  # newer meetings arrive while the reader pages
        seed_meeting(app_engine, owner.org_id, owner.user_id, None, started_at=T0 + timedelta(1))
    seen = [item["id"] for item in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        page = client.get(PATH, params={"cursor": cursor}).json()["sections"]["past"]
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]

    assert seen == expected


def test_page_size_bounds_and_default(app_engine: Engine) -> None:
    owner = new_member(app_engine)
    client = sign_in_owner(app_engine, owner)
    for n in range(30):
        seed_meeting(app_engine, owner.org_id, owner.user_id, None,
                     started_at=T0 - timedelta(minutes=n))

    default = client.get(PATH, params={"section": "past"}).json()["sections"]["past"]
    widest = client.get(PATH, params={"section": "past", "limit": 50}).json()["sections"]["past"]
    every = client.get(PATH).json()["sections"]

    assert len(default["items"]) == 25 and default["next_cursor"]
    assert len(widest["items"]) == 30 and widest["next_cursor"] is None
    assert len(every["past"]["items"]) == 25, "each section is capped"
    for limit in (0, 51, -1):
        assert client.get(PATH, params={"limit": limit}).status_code == 422


@pytest.mark.parametrize(
    "cursor",
    ["garbage", "e30", "eyJzIjoibm9wZSJ9", "", "a" * 600],
    ids=["not base64", "empty object", "unknown section", "empty", "oversized"],
)
def test_a_bad_cursor_is_422(world: AccessOrg, cursor: str) -> None:
    response = world.client("admin").get(PATH, params={"cursor": cursor})

    assert (response.status_code, response.json()["code"]) == (422, "bad_cursor")


def test_a_cursor_that_disagrees_with_the_request_is_422(app_engine: Engine) -> None:
    owner = new_member(app_engine)
    client = sign_in_owner(app_engine, owner)
    for _ in range(3):
        seed_meeting(app_engine, owner.org_id, owner.user_id, None)
    cursor = client.get(PATH, params={"section": "past", "limit": 1}).json()["sections"]["past"][
        "next_cursor"
    ]

    for params in ({"section": "live"}, {"filter": "mine"}):
        response = client.get(PATH, params={"cursor": cursor, **params})
        assert (response.status_code, response.json()["code"]) == (422, "bad_cursor"), params
    assert client.get(PATH, params={"cursor": cursor, "section": "past"}).status_code == 200


def test_anonymous_is_401(world: AccessOrg) -> None:
    assert world.harness.client().get(PATH).status_code == 401


# ─── 1,000 meetings ───────────────────────────────────────────────────────────────────────────────

CALLS = 20
P95_BUDGET_S = 0.300


def _seed_thousand(w: AccessOrg) -> None:
    third = new_team(w.engine, w.org_id, {w.people["requester"].user_id: "member"})
    teams = (w.team, w.shared_team, third)
    requesters = [w.people[c].user_id for c in ("requester", "lead", "team_member", "unrelated")]
    statuses = ["done"] * 9 + ["failed", "cancelled"]
    live = list(SECTIONS["live"])
    rows, shares = [], []
    for n in range(1000):
        meeting_id = uuid.uuid4()
        if n < 20:
            status = live[n % 4]
        elif n < 50:
            status = "scheduled"
        else:
            status = statuses[n % len(statuses)]
        started = None if status == "scheduled" else T0 - timedelta(hours=n * 7)
        rows.append(
            {
                "id": meeting_id,
                "org_id": w.org_id,
                "team_id": teams[n % 3],
                "requested_by": requesters[n % len(requesters)],
                "platform": ("google_meet", "teams", "zoom")[n % 3],
                "join_url": f"https://zoom.us/j/{n}",
                "join_url_key": f"zoom.us/j/{meeting_id.hex}",
                "status": status,
                "failure_reason": "internal_error" if status == "failed" else None,
                "started_at": started,
                "ended_at": None if started is None else started + timedelta(minutes=30),
                "title": f"Meeting {n}",
            }
        )
        if n % 20 == 3:  # 50 shares: half to a user, half to a team
            grantee = (
                ("user", w.people["unrelated"].user_id) if n % 40 == 3 else ("team", third)
            )
            shares.append(
                {
                    "org_id": w.org_id,
                    "meeting_id": meeting_id,
                    "grantee_type": grantee[0],
                    "grantee_id": grantee[1],
                    "granted_by": rows[-1]["requested_by"],
                }
            )
    with transaction(w.engine, org_id=w.org_id) as tx:
        tx.execute(insert(Meeting.__table__), rows)
        tx.execute(insert(MeetingShare.__table__), shares)
    assert len(shares) == 50


def _p95(samples: list[float]) -> float:
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


def _plan(engine: Engine, w: AccessOrg, caller: str, section: str) -> dict:
    actor = w.actor(caller)
    statement = listing.query(actor, section, "all", listing.DEFAULT_LIMIT + 1, None)
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
    )
    with transaction(engine, org_id=actor.org_id, user_id=actor.user_id) as tx:
        raw = tx.connection().exec_driver_sql(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + str(compiled), compiled.params
        ).scalar_one()
    return (raw if isinstance(raw, list) else json.loads(raw))[0]


def _meetings_reads(node: dict) -> set[str]:
    """How the plan reads `meetings`: the names of its indexes used (index or bitmap scans), or
    `Seq Scan`."""
    found = set()
    index = node.get("Index Name", "")
    if index.split("_")[1:2] == ["meetings"]:
        found.add(index)
    if node.get("Relation Name") == "meetings" and node["Node Type"] == "Seq Scan":
        found.add("Seq Scan")
    for child in node.get("Plans", []):
        found |= _meetings_reads(child)
    return found


def test_a_thousand_meetings_list_within_300_ms_at_p95(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    w = access_org(app_engine)
    _seed_thousand(w)
    with superuser_engine.connect() as conn:
        conn.execute(text("ANALYZE meetings, meeting_shares, team_memberships"))
        conn.commit()

    timings = {}
    for caller in ("lead", "unrelated", "admin"):
        client = w.client(caller)
        assert client.get(PATH).status_code == 200  # warm the connection pool
        samples = []
        for _ in range(CALLS):
            start = time.perf_counter()
            response = client.get(PATH)
            samples.append(time.perf_counter() - start)
            assert response.status_code == 200
        timings[caller] = samples

    plans = {
        (caller, section): _plan(app_engine, w, caller, section)
        for caller in ("lead", "admin")
        for section in SECTIONS
    }
    report = {
        caller: {
            "p95_ms": round(_p95(s) * 1000, 1),
            "median_ms": round(statistics.median(s) * 1000, 1),
            "max_ms": round(max(s) * 1000, 1),
        }
        for caller, s in timings.items()
    }
    for (caller, section), plan in plans.items():
        report[f"{caller}/{section}"] = {
            "execution_ms": plan["Execution Time"],
            "meetings_read_by": sorted(_meetings_reads(plan["Plan"])),
        }
    print("\nLIST TIMING", json.dumps(report, indent=1))

    for caller, samples in timings.items():
        assert _p95(samples) <= P95_BUDGET_S, (caller, report[caller])
    # The access predicate's path (a member) reads each section through the list index. An admin
    # reads the whole org: the planner may scan when most of the table qualifies (Past here), and
    # that stays fast.
    for section in SECTIONS:
        assert _meetings_reads(plans[("lead", section)]["Plan"]) == {
            "ix_meetings_org_id_status_sort_time"
        }, report[f"lead/{section}"]
    for (caller, section), plan in plans.items():
        assert plan["Execution Time"] < 20, report[f"{caller}/{section}"]
