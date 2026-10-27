"""No org reaches another org's data through any andai-core route.

Two orgs, A and B, each with an owner, an admin, a member and a team lead, a team, a pending
invite, an enrolled TOTP, a platform principal and audit events — so every org-scoped table holds
rows of both. Every route the application serves must sit in exactly one case table (a new route
fails `test_every_route_is_classified` until it is classified):

* `ID_ROUTES` — path parameters filled with org B's ids, called as every role of org A: 403 or
  404, and nothing of B's in the body.
* `ID_LESS_ROUTES` — called as every role of org A: the status the role matrix gives that role,
  and nothing of B's in the body.
* `PUBLIC_ROUTES` — the named public allowlist, called anonymously and from an A session with B's
  identifiers: nothing of B's in the body.

After every call, B's rows in every table are exactly as they were.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from sqlalchemy import Engine, select, text, update

from andai_core.auth.passwords import hash_password
from andai_core.db.models import ConsentRecord, Invite, User
from andai_core.db.session import transaction
from andai_core.main import create_app
from andai_core.org.service import create_org
from conftest import (
    PASSWORD,
    Harness,
    Member,
    enroll,
    invite_user,
    make_harness,
    new_member,
    new_team,
    served,
    sign_in,
    zoom_link,
)

ROLES = ("owner", "admin", "member", "lead")


@dataclass
class SeededOrg:
    label: str
    org_id: uuid.UUID
    name: str
    slug: str
    members: dict[str, Member]
    team: uuid.UUID
    team_name: str
    invite_id: uuid.UUID
    invite_email: str
    invite_token: str
    principal_key: str
    member_totp: object
    meeting_id: uuid.UUID
    meeting_link: str
    values: set[str] = field(default_factory=set)

    @property
    def users(self) -> list[uuid.UUID]:
        return [m.user_id for m in self.members.values()]


def _activate_owner(engine: Engine, user_id: uuid.UUID, display_name: str) -> None:
    with transaction(engine) as tx:
        tx.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                status="active", password_hash=hash_password(PASSWORD), display_name=display_name
            )
        )


def _rename(engine: Engine, user_id: uuid.UUID, display_name: str) -> None:
    with transaction(engine) as tx:
        tx.execute(update(User).where(User.id == user_id).values(display_name=display_name))


def seed_org(harness: Harness, engine: Engine, label: str) -> SeededOrg:
    tag = uuid.uuid4().hex[:10]
    name = f"{label} Org {tag}"
    owner_email = f"{label.lower()}-owner-{tag}@example.test"
    created = create_org(
        engine,
        name=name,
        owner_email=owner_email,
        owner_display_name=None,
        admin=harness.services.platform_admin,
        keyring=harness.services.keyring,
        now=harness.clock(),
    )
    org_id = created.org_id
    _activate_owner(engine, created.owner_id, f"{label} Owner {tag}")
    members = {"owner": Member(org_id, created.owner_id, owner_email, PASSWORD)}
    for role, org_role in (("admin", "admin"), ("member", "member"), ("lead", "member")):
        member = new_member(engine, role=org_role, org_id=org_id)
        _rename(engine, member.user_id, f"{label} {role.title()} {tag}")
        members[role] = member
    team_name = f"{label} Team {tag}"
    team = new_team(
        engine,
        org_id,
        {members["lead"].user_id: "lead", members["member"].user_id: "member"},
        name=team_name,
    )
    invite_email = f"{label.lower()}-invitee-{tag}@example.test"
    invite_token = invite_user(
        engine, harness, org_id=org_id, email=invite_email, invited_by=created.owner_id
    )
    with transaction(engine, org_id=org_id) as tx:
        invite_id = tx.scalar(select(Invite.id).where(Invite.email == invite_email))
    # An enrolled member: an admin's 2FA reset would have something to remove.
    member_totp, _ = enroll(sign_in(harness, members["member"]), harness.clock)
    [principal_key] = [
        t["token"]
        for t in harness.admin_api.tokens.values()
        if t["user_id"] == created.platform_user_id
    ]
    # A live meeting the bot was admitted to, sent by the lead for the team, with its consent row.
    meeting_link = zoom_link(passcode=f"pw{tag}")
    sent = sign_in(harness, members["lead"]).post(
        "/api/v1/meetings/join", json={"join_url": meeting_link, "team_id": str(team)}
    )
    assert sent.status_code == 201, sent.text
    meeting_id = uuid.UUID(sent.json()["id"])
    native_meeting_id = sent.json()["native_meeting_id"]
    with transaction(engine, org_id=org_id) as tx:
        tx.execute(
            text("UPDATE meetings SET status = 'recording' WHERE id = :m"), {"m": meeting_id}
        )
        tx.add(
            ConsentRecord(
                meeting_id=meeting_id,
                org_id=org_id,
                bot_display_name="munshi.ai (recording)",
                disclosure_text=f"{label} notice {tag}",
                privacy_notice_url="http://portal.test/privacy",
                disclosure_channel="chat",
                admitted_at=harness.clock(),
            )
        )
    with transaction(engine, org_id=org_id) as tx:
        slug = tx.scalar(text("SELECT slug FROM orgs WHERE id = :o"), {"o": org_id})
    seeded = SeededOrg(
        label=label,
        org_id=org_id,
        name=name,
        slug=slug or "",
        members=members,
        team=team,
        team_name=team_name,
        invite_id=invite_id,
        invite_email=invite_email,
        invite_token=invite_token,
        principal_key=principal_key,
        member_totp=member_totp,
        meeting_id=meeting_id,
        meeting_link=meeting_link,
    )
    seeded.values = {
        str(meeting_id),
        meeting_link,
        f"pw{tag}",
        native_meeting_id,
        f"{label} notice {tag}",
        str(org_id),
        name,
        str(team),
        team_name,
        str(invite_id),
        invite_email,
        invite_token,
        principal_key,
        tag,
        *(str(m.user_id) for m in members.values()),
        *(m.email for m in members.values()),
    }
    return seeded


@dataclass
class World:
    harness: Harness
    a: SeededOrg
    b: SeededOrg

    def client(self, org: SeededOrg, role: str):
        """A `full` session of `role` in `org` (through the code step for the enrolled member)."""
        totp = org.member_totp if role == "member" else None
        return sign_in(self.harness, org.members[role], totp)


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> World:
    harness = make_harness(app_engine)
    a = seed_org(harness, app_engine, "Alpha")
    b = seed_org(harness, app_engine, "Beta")
    assert b.slug, "org B's slug is stored"
    b.values.add(b.slug)
    return World(harness, a, b)


def b_state(superuser_engine: Engine, b: SeededOrg) -> dict:
    """Everything stored for org B, read past row-level security."""
    queries = {
        "orgs": "SELECT name, slug, require_2fa FROM orgs WHERE id = :o",
        "org_memberships": "SELECT user_id, role FROM org_memberships WHERE org_id = :o",
        "teams": "SELECT id, name FROM teams WHERE org_id = :o",
        "team_memberships": "SELECT team_id, user_id, role FROM team_memberships WHERE org_id = :o",
        "invites": "SELECT id, email, role, expires_at, accepted_at FROM invites WHERE org_id = :o",
        "platform_principals": (
            "SELECT platform_user_id, key_ciphertext FROM platform_principals WHERE org_id = :o"
        ),
        "audit_events": "SELECT count(*) FROM audit_events WHERE org_id = :o",
        "users": "SELECT id, status, display_name, password_hash FROM users WHERE id = ANY(:u)",
        "user_totp": "SELECT user_id, confirmed_at FROM user_totp WHERE user_id = ANY(:u)",
        "recovery_codes": "SELECT count(*) FROM recovery_codes WHERE user_id = ANY(:u)",
        "sessions": "SELECT count(*) FROM sessions WHERE user_id = ANY(:u)",
        "meetings": (
            "SELECT id, status, failure_reason, platform_meeting_id, join_url, updated_at"
            " FROM meetings WHERE org_id = :o"
        ),
        "consent_records": (
            "SELECT meeting_id, disclosure_channel, removed_at, updated_at"
            " FROM consent_records WHERE org_id = :o"
        ),
    }
    params = {"o": b.org_id, "u": b.users}
    with superuser_engine.connect() as conn:
        return {
            table: sorted(tuple(map(str, row)) for row in conn.execute(text(sql), params))
            for table, sql in queries.items()
        }


def leaked(body: str, b: SeededOrg) -> list[str]:
    lowered = body.lower()
    return sorted(value for value in b.values if value and value.lower() in lowered)


def concrete(path: str, ids: dict[str, uuid.UUID]) -> str:
    for name, value in ids.items():
        path = path.replace("{" + name + "}", str(value))
    assert "{" not in path, path
    return path


# ─── (a) routes with ids: filled with org B's ids ─────────────────────────────────────────────────

# (method, path) -> list of (case name, path ids, body) built from org B
IdCase = Callable[[SeededOrg], list[tuple[str, dict[str, uuid.UUID], dict | None]]]
ID_ROUTES: dict[tuple[str, str], IdCase] = {
    ("DELETE", "/api/v1/org/users/{user_id}/mfa"): lambda b: [
        ("B member", {"user_id": b.members["member"].user_id}, None),
        ("B owner", {"user_id": b.members["owner"].user_id}, None),
    ],
    ("PATCH", "/api/v1/org/users/{user_id}"): lambda b: [
        ("promote B member", {"user_id": b.members["member"].user_id}, {"role": "admin"}),
        ("disable B admin", {"user_id": b.members["admin"].user_id}, {"status": "disabled"}),
        ("take B ownership", {"user_id": b.members["owner"].user_id}, {"role": "member"}),
    ],
    ("GET", "/api/v1/meetings/{meeting_id}"): lambda b: [
        ("read B meeting", {"meeting_id": b.meeting_id}, None),
    ],
    ("POST", "/api/v1/meetings/{meeting_id}/stop"): lambda b: [
        ("stop B meeting", {"meeting_id": b.meeting_id}, None),
    ],
    ("PUT", "/api/v1/teams/{team_id}/members"): lambda b: [
        ("empty B team", {"team_id": b.team}, {"members": []}),
        (
            "rewrite B team",
            {"team_id": b.team},
            {"members": [{"user_id": str(b.members["admin"].user_id), "role": "lead"}]},
        ),
    ],
}

# ─── (b) routes without ids: whatever the role matrix gives, only org A's data ────────────────────

ALL = frozenset(ROLES)
ADMINS = frozenset({"owner", "admin"})
CODE = {"code": "000000"}
# (method, path) -> (roles the matrix admits, body)
ID_LESS_ROUTES: dict[tuple[str, str], tuple[frozenset[str], dict | None]] = {
    ("GET", "/api/v1/me"): (ALL, None),
    ("POST", "/api/v1/me/mfa"): (ALL, None),
    ("POST", "/api/v1/me/mfa/confirm"): (ALL, CODE),
    ("DELETE", "/api/v1/me/mfa"): (ALL, CODE),
    ("POST", "/api/v1/me/mfa/recovery-codes"): (ALL, CODE),
    ("GET", "/api/v1/org/security"): (ALL, None),
    ("PUT", "/api/v1/org/security"): (ADMINS, {"require2fa": False}),
    ("GET", "/api/v1/org/users"): (ADMINS, None),
    ("POST", "/api/v1/org/users"): (ADMINS, "invite"),
    ("GET", "/api/v1/teams"): (ALL, None),
    ("POST", "/api/v1/teams"): (ADMINS, "team"),
    ("POST", "/api/v1/meetings/join"): (ALL, "join"),
}

# ─── (c) the public allowlist, called with org B's identifiers ────────────────────────────────────

PublicCase = Callable[[SeededOrg], dict | None]
PUBLIC_ROUTES: dict[tuple[str, str], PublicCase] = {
    ("POST", "/api/v1/auth/login"): lambda b: {
        "email": b.members["member"].email,
        "password": "not the password of anyone",
    },
    ("POST", "/api/v1/auth/logout"): lambda b: None,
    ("POST", "/api/v1/auth/password-reset"): lambda b: {"email": b.members["owner"].email},
    ("POST", "/api/v1/auth/password-reset/confirm"): lambda b: {
        "token": str(b.invite_id),
        "password": "a brand new passphrase",
    },
    ("POST", "/api/v1/auth/invite/accept"): lambda b: {
        "token": str(b.invite_id),
        "password": "a brand new passphrase",
    },
    ("POST", "/api/v1/auth/mfa/verify"): lambda b: CODE,
    ("POST", "/api/v1/bootstrap"): lambda b: {
        "email": b.members["owner"].email,
        "org_name": b.name,
    },
    ("GET", "/healthz"): lambda b: None,
}

ROUTES = sorted(served(create_app()))


def test_every_route_is_classified() -> None:
    tables = (set(ID_ROUTES), set(ID_LESS_ROUTES), set(PUBLIC_ROUTES))
    classified = set().union(*tables)
    assert sum(len(t) for t in tables) == len(classified), "a route is in two case tables"
    assert set(ROUTES) - classified == set(), "unclassified routes: add them to a case table"
    assert classified - set(ROUTES) == set(), "classified routes that no longer exist"
    assert all("{" in path for _, path in ID_ROUTES)
    assert not any("{" in path for _, path in (*ID_LESS_ROUTES, *PUBLIC_ROUTES))


def test_the_world_puts_rows_of_both_orgs_in_every_org_scoped_table(
    world: World, superuser_engine: Engine
) -> None:
    from andai_core.db.models import ORG_SCOPED_TABLES

    with superuser_engine.connect() as conn:
        for table in ORG_SCOPED_TABLES:
            column = "id" if table == "orgs" else "org_id"
            for org in (world.a, world.b):
                count = conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {column} = :o"), {"o": org.org_id}
                ).scalar_one()
                assert count >= 1, (table, org.label)


def _call(client, method: str, path: str, body):
    return client.request(method, path, **({} if body is None else {"json": body}))


ID_CASES = [
    pytest.param(method, path, role, id=f"{method} {path} as A {role}")
    for (method, path) in sorted(ID_ROUTES)
    for role in ROLES
]


@pytest.mark.parametrize(("method", "path", "role"), ID_CASES)
def test_id_routes_refuse_org_b_ids(
    world: World, superuser_engine: Engine, method: str, path: str, role: str
) -> None:
    before = b_state(superuser_engine, world.b)
    client = world.client(world.a, role)

    for case, ids, body in ID_ROUTES[(method, path)](world.b):
        response = _call(client, method, concrete(path, ids), body)

        assert response.status_code in (403, 404), (case, response.status_code, response.text)
        assert leaked(response.text, world.b) == [], case
    assert b_state(superuser_engine, world.b) == before


INVITE_OR_TEAM = {
    "invite": lambda: {"email": f"fresh-{uuid.uuid4().hex[:8]}@example.test", "role": "member"},
    "team": lambda: {"name": f"Fresh {uuid.uuid4().hex[:8]}"},
    "join": lambda: {"join_url": zoom_link()},
}

ID_LESS_CASES = [
    pytest.param(method, path, role, id=f"{method} {path} as A {role}")
    for (method, path) in sorted(ID_LESS_ROUTES)
    for role in ROLES
]


@pytest.mark.parametrize(("method", "path", "role"), ID_LESS_CASES)
def test_id_less_routes_answer_with_org_a_data_only(
    world: World, superuser_engine: Engine, method: str, path: str, role: str
) -> None:
    before = b_state(superuser_engine, world.b)
    client = world.client(world.a, role)
    admitted, body = ID_LESS_ROUTES[(method, path)]
    if isinstance(body, str):
        body = INVITE_OR_TEAM[body]()

    response = _call(client, method, path, body)

    if role in admitted:
        assert response.status_code not in (401, 403), response.text
        assert response.status_code < 500, response.text
    else:
        assert response.status_code == 403, response.text
    assert leaked(response.text, world.b) == []
    assert b_state(superuser_engine, world.b) == before


def test_org_a_reads_list_org_a_and_never_org_b(world: World) -> None:
    """The positive half: the id-less reads do return A's data (so the leak check above is not
    passing on empty bodies)."""
    owner = world.client(world.a, "owner")

    users = owner.get("/api/v1/org/users").json()
    assert {u["id"] for u in users["users"]} == {str(u) for u in world.a.users}
    # The calls above may have added invites and teams to A; A's seeded ones are among them.
    assert world.a.invite_email in [i["email"] for i in users["invites"]]
    teams = owner.get("/api/v1/teams").json()["teams"]
    assert str(world.a.team) in [t["id"] for t in teams]
    assert str(world.b.team) not in [t["id"] for t in teams]
    me = owner.get("/api/v1/me").json()
    assert me["org"]["id"] == str(world.a.org_id)


PUBLIC_CASES = [
    pytest.param(method, path, who, id=f"{method} {path} {who}")
    for (method, path) in sorted(PUBLIC_ROUTES)
    for who in ("anonymous", "as A owner")
]


@pytest.mark.parametrize(("method", "path", "who"), PUBLIC_CASES)
def test_public_routes_return_nothing_of_org_b(
    world: World, superuser_engine: Engine, method: str, path: str, who: str
) -> None:
    before = b_state(superuser_engine, world.b)
    if who == "anonymous":
        client = world.harness.client()
    else:
        client = world.client(world.a, "owner")
    outbox = len(world.harness.mailer.outbox)

    response = _call(client, method, path, PUBLIC_ROUTES[(method, path)](world.b))

    assert response.status_code < 500, response.text
    assert leaked(response.text, world.b) == []
    assert b_state(superuser_engine, world.b) == before
    # Only a reset request mails anyone, and then only the address's own owner.
    sent = world.harness.mailer.outbox[outbox:]
    expected = [world.b.members["owner"].email] if path.endswith("/password-reset") else []
    assert [m.to for m in sent] == expected


def test_inviting_an_address_of_org_b_reveals_nothing(
    world: World, superuser_engine: Engine
) -> None:
    before = b_state(superuser_engine, world.b)
    admin = world.client(world.a, "admin")
    email = world.b.members["member"].email

    response = admin.post("/api/v1/org/users", json={"email": email, "role": "member"})

    # Answered exactly like a fresh address; the only B value in it is the one A typed.
    assert response.status_code == 201
    assert set(response.json()) == {"id", "email", "role", "expires_at"}
    assert leaked(response.text, world.b) == [email]
    assert b_state(superuser_engine, world.b) == before


def test_org_b_people_cannot_be_put_in_an_org_a_team(
    world: World, superuser_engine: Engine
) -> None:
    before = b_state(superuser_engine, world.b)
    admin = world.client(world.a, "admin")
    body = {
        "members": [
            {"user_id": str(world.a.members["lead"].user_id), "role": "lead"},
            {"user_id": str(world.b.members["member"].user_id), "role": "member"},
        ]
    }

    response = admin.put(f"/api/v1/teams/{world.a.team}/members", json=body)

    assert response.status_code == 422
    assert leaked(response.text, world.b) == []
    assert b_state(superuser_engine, world.b) == before


def test_a_session_of_org_b_sees_org_b_and_not_org_a(world: World) -> None:
    """The suite is symmetric: B's owner sees B's data, none of A's."""
    owner = world.client(world.b, "owner")
    for path in ("/api/v1/me", "/api/v1/org/users", "/api/v1/teams", "/api/v1/org/security"):
        response = owner.get(path)
        assert response.status_code == 200
        assert leaked(response.text, world.a) == [], path
    assert str(world.b.org_id) in owner.get("/api/v1/me").text


def test_org_b_meeting_link_and_team_reach_nothing_of_org_b(
    world: World, superuser_engine: Engine
) -> None:
    """A pastes B's live link (A gets its own meeting, nothing of B's), and names B's team (404)."""
    before = b_state(superuser_engine, world.b)
    dispatched = len(world.harness.gateway.dispatches)
    admin = world.client(world.a, "admin")

    own = admin.post("/api/v1/meetings/join", json={"join_url": world.b.meeting_link})
    team = admin.post(
        "/api/v1/meetings/join", json={"join_url": zoom_link(), "team_id": str(world.b.team)}
    )

    assert own.status_code == 201, own.text
    assert own.json()["id"] != str(world.b.meeting_id)
    # The only B values in A's answer are parts of the link A typed.
    assert all(value in world.b.meeting_link for value in leaked(own.text, world.b))
    assert (team.status_code, team.json()["code"]) == (404, "no_such_team")
    assert b_state(superuser_engine, world.b) == before
    # A's bot went out under A's own principal.
    assert len(world.harness.gateway.dispatches) == dispatched + 1
    a_platform = world.harness.gateway.meetings[
        max(world.harness.gateway.meetings)
    ]
    b_user = next(
        t["user_id"] for t in world.harness.admin_api.tokens.values()
        if t["token"] == world.b.principal_key
    )
    assert a_platform["user_id"] != b_user
