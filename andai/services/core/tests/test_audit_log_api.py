"""`GET /api/v1/org/audit` and `/org/audit/actions`: owners and admins page, filter and read
their own org's trail; nobody else does."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from andai_core.audit_actions import ACTIONS
from conftest import Harness, new_team, role_org, seed_meeting, sign_in

AUDIT = "/api/v1/org/audit"
ACTIONS_PATH = "/api/v1/org/audit/actions"
SEEDED = {"share.grant", "share.revoke", "member.role_change", "team.members_set", "meeting.delete"}


def _insert(
    superuser_engine: Engine,
    org_id: uuid.UUID,
    rows: list[tuple[datetime, str, str, uuid.UUID | None]],
    ip: str | None = "203.0.113.9",
) -> list[uuid.UUID]:
    """(at, action, target, actor) rows at chosen times (the app role's inserts take now())."""
    ids = []
    with superuser_engine.begin() as conn:
        for at, action, target, actor in rows:
            event_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO audit_events (id, org_id, actor_user_id, action, target, at, ip)"
                    " VALUES (:i, :o, :u, :a, :t, :at, CAST(:ip AS inet))"
                ),
                {"i": event_id, "o": org_id, "u": actor, "a": action, "t": target, "at": at,
                 "ip": ip},
            )
            ids.append(event_id)
    return ids


def _day(harness: Harness, days_ago: int, hour: int = 12) -> datetime:
    today = harness.clock().astimezone(UTC).date()
    day = today - timedelta(days=days_ago)
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _seeded(items: list[dict]) -> list[dict]:
    return [item for item in items if item["action"] in SEEDED]


@pytest.fixture
def org(harness: Harness, app_engine: Engine):
    return role_org(app_engine)


# ─── access ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [AUDIT, ACTIONS_PATH])
@pytest.mark.parametrize(("role", "status"), [("owner", 200), ("admin", 200), ("member", 403),
                                              ("lead", 403)])
def test_only_owners_and_admins_read_the_trail(
    harness: Harness, org, path: str, role: str, status: int
) -> None:
    assert sign_in(harness, org.by_role(role)).get(path).status_code == status


def test_signed_out_is_401(harness: Harness) -> None:
    assert harness.client().get(AUDIT).status_code == 401


# ─── the page ─────────────────────────────────────────────────────────────────────────────────────


def test_items_carry_actor_action_target_and_ip_newest_first(
    harness: Harness, app_engine: Engine, superuser_engine: Engine, org
) -> None:
    meeting = seed_meeting(
        app_engine, org.owner.org_id, org.lead.user_id, org.team, title="Quarterly review"
    )
    gone = seed_meeting(app_engine, org.owner.org_id, org.lead.user_id, org.team, title="Gone")
    with superuser_engine.begin() as conn:
        conn.execute(text("UPDATE meetings SET deleted_at = now(), deletion_state = 'pending' WHERE id = :m"), {"m": gone})
    stranger = uuid.uuid4()  # an actor with no user row: an unknown (or erased) user
    _insert(
        superuser_engine,
        org.owner.org_id,
        [
            (_day(harness, 3), "member.role_change", f"user:{org.member.user_id}",
             org.admin.user_id),
            (_day(harness, 2), "team.members_set", f"team:{org.team}", org.lead.user_id),
            (_day(harness, 1), "share.grant",
             f"meeting:{meeting} \N{RIGHTWARDS ARROW} user:{org.admin.user_id}", org.owner.user_id),
            (_day(harness, 1, 13), "meeting.delete", f"meeting:{gone}", stranger),
        ],
    )

    response = sign_in(harness, org.admin).get(AUDIT)

    assert response.status_code == 200
    body = response.json()
    items = _seeded(body["items"])
    assert [item["action"] for item in items] == [
        "meeting.delete", "share.grant", "team.members_set", "member.role_change"
    ]
    assert body["next"] is None
    deleted, share, team, role = items
    assert deleted["actor"] == {"id": str(stranger), "name": None}
    assert deleted["target"] == {"type": "meeting", "id": str(gone), "label": None}
    assert share["actor"] == {"id": str(org.owner.user_id), "name": "Test Member"}
    assert share["target"] == {"type": "meeting", "id": str(meeting), "label": "Quarterly review"}
    assert share["category"] == "sharing" and share["ip"] == "203.0.113.9"
    assert share["at"] == _day(harness, 1).isoformat()
    assert team["target"]["label"].startswith("Team ")
    assert role["target"] == {
        "type": "user", "id": str(org.member.user_id), "label": "Test Member"
    }
    assert set(share) == {"id", "at", "actor", "action", "category", "target", "ip"}


def test_sign_ins_appear_in_the_trail(harness: Harness, org) -> None:
    client = sign_in(harness, org.owner)
    items = client.get(AUDIT, params={"action": "login.success"}).json()["items"]
    assert items and items[0]["actor"]["id"] == str(org.owner.user_id)
    assert items[0]["target"]["id"] == str(org.owner.user_id)


def test_default_range_is_the_last_30_days(
    harness: Harness, superuser_engine: Engine, org
) -> None:
    org_id = org.owner.org_id
    inside, edge, outside = _insert(
        superuser_engine,
        org_id,
        [
            (_day(harness, 0, 0), "share.grant", f"org:{org_id}", None),
            (_day(harness, 29, 0), "share.grant", f"org:{org_id}", None),
            (_day(harness, 30, 23), "share.grant", f"org:{org_id}", None),
        ],
    )
    ids = [i["id"] for i in _seeded(sign_in(harness, org.owner).get(AUDIT).json()["items"])]
    assert ids == [str(inside), str(edge)]
    assert str(outside) not in ids


def test_from_and_to_are_whole_inclusive_utc_days(
    harness: Harness, superuser_engine: Engine, org
) -> None:
    org_id = org.owner.org_id
    first = _day(harness, 10, 0).replace(hour=0)
    last = _day(harness, 8).replace(hour=23, minute=59, second=59)
    before, start, end, after = _insert(
        superuser_engine,
        org_id,
        [
            (first - timedelta(seconds=1), "share.grant", f"org:{org_id}", None),
            (first, "share.grant", f"org:{org_id}", None),
            (last, "share.revoke", f"org:{org_id}", None),
            (last + timedelta(seconds=1), "share.grant", f"org:{org_id}", None),
        ],
    )
    params = {"from": first.date().isoformat(), "to": last.date().isoformat()}
    ids = [i["id"] for i in sign_in(harness, org.owner).get(AUDIT, params=params).json()["items"]]
    assert ids == [str(end), str(start)]


def test_action_filter(harness: Harness, superuser_engine: Engine, org) -> None:
    org_id = org.owner.org_id
    grant, revoke, delete = _insert(
        superuser_engine,
        org_id,
        [
            (_day(harness, 2), "share.grant", f"org:{org_id}", None),
            (_day(harness, 1), "share.revoke", f"org:{org_id}", None),
            (_day(harness, 0, 1), "meeting.delete", f"org:{org_id}", None),
        ],
    )
    client = sign_in(harness, org.owner)
    one = client.get(AUDIT, params={"action": "share.grant"}).json()["items"]
    assert [i["id"] for i in one] == [str(grant)]
    two = client.get(AUDIT, params={"action": " share.revoke,meeting.delete "}).json()["items"]
    assert [i["id"] for i in two] == [str(delete), str(revoke)]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"action": "share.grant,share.granted"}, "unknown_action"),
        ({"from": "2026-01-10", "to": "2026-01-09"}, "bad_range"),
        ({"from": "2025-01-01", "to": "2026-01-02"}, "bad_range"),  # 367 days
        ({"cursor": "not-a-cursor"}, "bad_cursor"),
        ({"cursor": "MjAyNi0wMS0wMXxub3Bl"}, "bad_cursor"),  # "2026-01-01|nope"
    ],
)
def test_bad_filters_are_422(harness: Harness, org, params: dict, code: str) -> None:
    response = sign_in(harness, org.owner).get(AUDIT, params=params)
    assert response.status_code == 422
    assert response.json()["code"] == code


def test_a_366_day_range_is_allowed(harness: Harness, org) -> None:
    params = {"from": "2025-01-01", "to": "2026-01-01"}
    assert sign_in(harness, org.owner).get(AUDIT, params=params).status_code == 200


def test_bad_dates_are_422(harness: Harness, org) -> None:
    response = sign_in(harness, org.owner).get(AUDIT, params={"from": "yesterday"})
    assert response.status_code == 422


def test_cursor_pages_230_events_without_duplicates(
    harness: Harness, superuser_engine: Engine, org
) -> None:
    org_id = org.owner.org_id
    # Several events share each second, so the id breaks ties across page boundaries.
    rows = [
        (_day(harness, 5) + timedelta(seconds=i // 4), "share.grant", f"org:{org_id}", None)
        for i in range(230)
    ]
    seeded = {str(event_id) for event_id in _insert(superuser_engine, org_id, rows)}
    client = sign_in(harness, org.owner)

    seen: list[str] = []
    sizes = []
    params = {"action": "share.grant"}
    while True:
        body = client.get(AUDIT, params=params).json()
        sizes.append(len(body["items"]))
        times = [(i["at"], i["id"]) for i in body["items"]]
        assert times == sorted(times, reverse=True)
        seen.extend(i["id"] for i in body["items"])
        if body["next"] is None:
            break
        params = {"action": "share.grant", "cursor": body["next"]}
    assert sizes == [50, 50, 50, 50, 30]
    assert len(seen) == len(set(seen)) == 230
    assert set(seen) == seeded


def test_another_orgs_events_never_appear(
    harness: Harness, app_engine: Engine, superuser_engine: Engine, org
) -> None:
    other = role_org(app_engine)
    mine = _insert(superuser_engine, org.owner.org_id, [(_day(harness, 1), "share.grant",
                                                         f"org:{org.owner.org_id}", None)])
    theirs = _insert(superuser_engine, other.owner.org_id, [(_day(harness, 1), "share.grant",
                                                             f"org:{other.owner.org_id}", None)])
    sign_in(harness, other.owner)  # a sign-in in the other org too
    response = sign_in(harness, org.owner).get(AUDIT)
    ids = {i["id"] for i in response.json()["items"]}
    assert str(mine[0]) in ids and str(theirs[0]) not in ids
    assert str(other.owner.org_id) not in response.text
    assert str(other.owner.user_id) not in response.text


def test_a_user_target_outside_the_org_is_not_named(
    harness: Harness, app_engine: Engine, superuser_engine: Engine, org
) -> None:
    outsider = role_org(app_engine).owner
    _insert(
        superuser_engine,
        org.owner.org_id,
        [(_day(harness, 1), "member.role_change", f"user:{outsider.user_id}", None)],
    )
    items = _seeded(sign_in(harness, org.owner).get(AUDIT).json()["items"])
    assert items[0]["target"] == {"type": "user", "id": str(outsider.user_id), "label": None}


def test_a_team_of_another_org_is_not_named(
    harness: Harness, app_engine: Engine, superuser_engine: Engine, org
) -> None:
    other = role_org(app_engine)
    foreign = new_team(app_engine, other.owner.org_id, {}, name="Foreign team")
    _insert(
        superuser_engine,
        org.owner.org_id,
        [(_day(harness, 1), "team.members_set", f"team:{foreign}", None)],
    )
    response = sign_in(harness, org.owner).get(AUDIT)
    assert _seeded(response.json()["items"])[0]["target"]["label"] is None
    assert "Foreign team" not in response.text


# ─── the action list ──────────────────────────────────────────────────────────────────────────────


def test_actions_lists_the_registry(harness: Harness, org) -> None:
    body = sign_in(harness, org.admin).get(ACTIONS_PATH).json()
    assert body == [
        {"key": key, "category": entry.category, "label": entry.label}
        for key, entry in ACTIONS.items()
    ]
