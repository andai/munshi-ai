"""Paste-link dispatch end to end through andai-core's own API, against the running stack with the
mock bot (marked `live`).

Needs the andai compose stack up with `BROWSER_IMAGE=mock-bot:dev`, andai-core's reconciler on, and
the env file named by `ENV_FILE` (skips without it). It creates one org through `create_org` (real
admin-api), gives its owner a password, signs in through andai-core, and for each mock scenario
pastes a Zoom-shaped link whose meeting number is unique to the scenario and the run (only the
`mock=` query parameter carries the scenario):

| scenario       | andai outcome                                                        |
|----------------|----------------------------------------------------------------------|
| `normal`       | `done`, consent `chat` with `posted_at`                              |
| `reject`       | `failed` / `admission_denied`                                        |
| `join-timeout` | `failed` / `admission_timeout`                                       |
| `left-alone`   | `done` with end reason `left_alone`                                  |
| `evicted`      | `done`, consent `removed_at`, and pasting the link again is 409      |
|                | `removed_by_participant`                                             |

While each runs, the test polls the platform meeting (with the org's key, through the gateway)
and andai's row (as the database superuser) every 200 ms. Every status andai reaches after
`dispatching` must appear within 5 s of the platform's own transition into the matching state
(the platform's `data.status_transition` timestamps, read when the meeting is over). The org, its
meetings, users and tokens are removed afterwards.

    ENV_FILE=/abs/env.smoke ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q -m live \\
        services/core/tests/test_meetings_live.py
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text, update

from andai_core.auth.passwords import hash_password
from andai_core.crypto import Keyring
from andai_core.db.models import User
from andai_core.db.session import transaction
from andai_core.meetings.status_map import map_platform
from andai_core.org.service import create_org
from andai_core.platform import principals
from andai_core.platform.admin_client import ADMIN_KEY_HEADER, HttpPlatformAdmin
from conftest import zoom_link
from test_principals_live import admin_url, engines, stack  # noqa: F401

pytestmark = pytest.mark.live

PASSWORD = "live meetings passphrase 0404"
TERMINAL = {"done", "failed", "cancelled"}
LATENCY_BUDGET_S = 5.0
SCENARIO_TIMEOUT_S = 120.0
POLL_S = 0.2


@dataclass
class LiveOrg:
    org_id: uuid.UUID
    owner_email: str
    platform_user_id: int
    key: str = field(repr=False)
    client: httpx.Client


@pytest.fixture(scope="module")
def core_url(stack: dict[str, str]) -> str:
    return f"http://127.0.0.1:{stack.get('ANDAI_CORE_PORT') or 18300}"


@pytest.fixture(scope="module")
def gateway(stack: dict[str, str]) -> Iterator[httpx.Client]:
    port = stack.get("API_GATEWAY_HOST_PORT") or 18056
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
        yield client


@pytest.fixture(scope="module")
def org(
    stack: dict[str, str], admin_url: str, engines: tuple[Engine, Engine], core_url: str
) -> Iterator[LiveOrg]:
    app, superuser = engines
    admin = HttpPlatformAdmin(admin_url, SecretStr(stack["ADMIN_TOKEN"]))
    keyring = Keyring(base64.b64decode(stack["ANDAI_KEK"]))
    owner_email = f"live-meetings-{uuid.uuid4().hex[:12]}@example.test"
    created = create_org(
        app,
        name=f"Live meetings {uuid.uuid4().hex[:8]}",
        owner_email=owner_email,
        owner_display_name="Live Owner",
        admin=admin,
        keyring=keyring,
        now=datetime.now(UTC),
    )
    with transaction(app) as tx:
        tx.execute(
            update(User)
            .where(User.id == created.owner_id)
            .values(status="active", password_hash=hash_password(PASSWORD))
        )
    with transaction(app, org_id=created.org_id) as tx:
        key = principals.open_key(tx, keyring, created.org_id)
    origin = stack.get("ANDAI_PORTAL_ORIGIN") or "http://127.0.0.1:13100"
    client = httpx.Client(base_url=core_url, timeout=60, headers={"Origin": origin})
    try:
        login = client.post(
            "/api/v1/auth/login", json={"email": owner_email, "password": PASSWORD}
        )
        assert login.status_code == 200 and login.json()["stage"] == "full", login.text
        yield LiveOrg(created.org_id, owner_email, created.platform_user_id, key, client)
    finally:
        client.close()
        _cleanup(stack, admin_url, superuser, created.org_id, owner_email, created.platform_user_id)


def _cleanup(
    stack: dict[str, str],
    admin_url: str,
    superuser: Engine,
    org_id: uuid.UUID,
    owner_email: str,
    platform_user_id: int,
) -> None:
    headers = {ADMIN_KEY_HEADER: stack["ADMIN_TOKEN"]}
    with httpx.Client(base_url=admin_url, timeout=10) as client:
        tokens = client.get(f"/admin/users/{platform_user_id}/tokens", headers=headers)
        for token in tokens.json() if tokens.status_code == 200 else []:
            client.delete(f"/admin/tokens/{token['id']}", headers=headers)
    with superuser.begin() as conn:
        for table in (
            "consent_records", "meetings", "audit_events", "platform_principals",
            "team_memberships", "teams", "invites", "org_memberships",
        ):
            conn.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id})
        conn.execute(text("DELETE FROM sessions WHERE org_id = :o"), {"o": org_id})
        conn.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org_id})
        conn.execute(text("DELETE FROM users WHERE email = :e"), {"e": owner_email})


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class Run:
    meeting: dict
    final: dict
    platform: dict
    # andai status -> when this test first saw it in the andai row
    andai_seen: dict[str, datetime]


def run_scenario(
    org: LiveOrg, gateway: httpx.Client, superuser: Engine, link: str
) -> Run:
    joined = org.client.post("/api/v1/meetings/join", json={"join_url": link, "language": "en"})
    assert joined.status_code == 201, joined.text
    meeting = joined.json()
    meeting_id = meeting["id"]
    andai_seen: dict[str, datetime] = {}
    platform_id = None
    deadline = time.monotonic() + SCENARIO_TIMEOUT_S
    status = meeting["status"]
    while time.monotonic() < deadline:
        with superuser.connect() as conn:
            row = conn.execute(
                text("SELECT status, platform_meeting_id FROM meetings WHERE id = :m"),
                {"m": meeting_id},
            ).one()
        now = datetime.now(UTC)
        status, platform_id = row
        andai_seen.setdefault(status, now)
        if status in TERMINAL:
            break
        time.sleep(POLL_S)
    assert status in TERMINAL, f"{link.split('?')[0]}: still {status} after {SCENARIO_TIMEOUT_S}s"
    final = org.client.get(f"/api/v1/meetings/{meeting_id}")
    assert final.status_code == 200, final.text
    platform = gateway.get(f"/meetings/{platform_id}", headers={"X-API-Key": org.key})
    assert platform.status_code == 200, platform.status_code
    return Run(meeting, final.json(), platform.json(), andai_seen)


def platform_transitions(platform: dict) -> list[tuple[str, datetime]]:
    """(platform status, when it was entered), oldest first."""
    transitions = (platform.get("data") or {}).get("status_transition") or []
    out = []
    for t in transitions:
        to, at = t.get("to"), t.get("timestamp")
        if isinstance(to, str) and isinstance(at, str):
            out.append((to, _parse(at)))
    return out


def assert_within_budget(run: Run) -> dict[str, float]:
    """Each andai status after `dispatching` appears within 5 s of the platform transition that
    leads to it. Returns the measured lags (seconds)."""
    transitions = platform_transitions(run.platform)
    assert transitions, "the platform meeting has no status_transition history"
    final_status = run.final["status"]
    lags: dict[str, float] = {}
    for status, seen in run.andai_seen.items():
        if status == "dispatching":
            continue
        # The platform transitions whose mapped andai status is this one (terminal: the last).
        matching = [
            at for to, at in transitions
            if _andai_status_of(to, run.platform) == status
        ]
        assert matching, (status, transitions)
        entered = matching[-1] if status == final_status else matching[0]
        lag = (seen - entered).total_seconds()
        lags[status] = round(lag, 2)
        assert lag <= LATENCY_BUDGET_S, (status, lag, transitions)
    return lags


def _andai_status_of(platform_status: str, platform: dict) -> str:
    if platform_status in ("completed", "failed"):
        return map_platform(platform).status  # the final record decides the terminal mapping
    return map_platform({"status": platform_status, "start_time": None, "data": {}}).status


EXPECTED = {
    "normal": {"status": "done", "end_reason": "stopped", "failure_reason": None},
    "reject": {"status": "failed", "failure_reason": "admission_denied"},
    "join-timeout": {"status": "failed", "failure_reason": "admission_timeout"},
    "left-alone": {"status": "done", "end_reason": "left_alone", "failure_reason": None},
    "evicted": {"status": "done", "end_reason": "evicted", "failure_reason": None},
}


@pytest.mark.parametrize("scenario", list(EXPECTED))
def test_mock_scenario_through_andai_core(
    scenario: str, org: LiveOrg, gateway: httpx.Client, engines: tuple[Engine, Engine]
) -> None:
    _, superuser = engines
    link = zoom_link(scenario)

    run = run_scenario(org, gateway, superuser, link)

    final = run.final
    for name, value in EXPECTED[scenario].items():
        assert final[name] == value, (scenario, name, final)
    assert final["platform"] == "zoom"
    assert final["native_meeting_id"] == link.split("/j/")[1].split("?")[0]
    lags = assert_within_budget(run)
    print(f"\n{scenario}: andai statuses {sorted(run.andai_seen)} lags_s={lags}")

    consent = final["consent"]
    if scenario in ("reject", "join-timeout"):
        assert consent is None
    else:
        assert consent is not None, final
        assert consent["bot_display_name"] == "munshi.ai (recording)"
    if scenario == "normal":
        assert consent["disclosure_channel"] == "chat"
        assert consent["disclosure_posted_at"] is not None
        assert consent["removed_at"] is None
    if scenario == "evicted":
        assert consent["removed_at"] is not None
        again = org.client.post("/api/v1/meetings/join", json={"join_url": link})
        assert again.status_code == 409, again.text
        assert again.json()["code"] == "removed_by_participant"
