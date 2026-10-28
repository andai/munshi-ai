"""The live relay end to end through andai-core, against the running stack with the mock bot
(marked `live`).

Needs the andai compose stack up with `BROWSER_IMAGE=mock-bot:dev`, andai-core's reconciler on, and
the env file named by `ENV_FILE` (skips without it). It creates an org (and a second one, for the
refusal), signs its owner in through andai-core, pastes a Zoom-shaped link with a meeting number
unique to the run and `mock=emit-n-segments` (the mock scenario that speaks 12 segments), and at
once opens `GET /api/v1/meetings/{id}/live` as the requester. It reads events until the stream
ends and checks:

* the status events end in `done`, and each one after the first arrived within 5 s of the
  platform's own transition into the matching state (`data.status_transition`);
* at least 12 segments arrived, each with a speaker and its timestamps;
* `GET /api/v1/meetings/{id}/transcript` lists the same segment ids;
* the owner of another org gets 404 from the stream.

Each segment's arrival minus its `absolute_end` is printed (the mock's segment times are synthetic
offsets from the meeting's start, so this lag is informational, not the speech-to-text latency).

    ENV_FILE=/abs/env ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q -s -m live \\
        services/core/tests/test_live_relay_live.py
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text, update

from andai_core.auth.passwords import hash_password
from andai_core.crypto import Keyring
from andai_core.db.models import User
from andai_core.db.session import transaction
from andai_core.org.service import create_org
from andai_core.platform import principals
from andai_core.platform.admin_client import HttpPlatformAdmin
from conftest import zoom_link
from test_meetings_live import (  # noqa: F401
    LATENCY_BUDGET_S,
    LiveOrg,
    _andai_status_of,
    _cleanup,
    _parse,
    core_url,
    gateway,
    platform_transitions,
)
from test_principals_live import admin_url, engines, stack  # noqa: F401

pytestmark = pytest.mark.live

PASSWORD = "live relay passphrase 0405"
STREAM_TIMEOUT_S = 120.0
MIN_SEGMENTS = 12


@contextmanager
def live_org(
    stack: dict[str, str], admin_url: str, engines: tuple[Engine, Engine], core_url: str,
    label: str,
) -> Iterator[LiveOrg]:
    """A fresh org whose owner is signed in to andai-core; removed afterwards."""
    app, superuser = engines
    admin = HttpPlatformAdmin(admin_url, SecretStr(stack["ADMIN_TOKEN"]))
    keyring = Keyring(base64.b64decode(stack["ANDAI_KEK"]))
    owner_email = f"live-relay-{label}-{uuid.uuid4().hex[:12]}@example.test"
    created = create_org(
        app,
        name=f"Live relay {label} {uuid.uuid4().hex[:8]}",
        owner_email=owner_email,
        owner_display_name="Live Relay Owner",
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


def read_stream(client: httpx.Client, path: str) -> tuple[list[tuple[str, dict, datetime]], str]:
    """Every event of the stream with its arrival time, and the raw text."""
    events: list[tuple[str, dict, datetime]] = []
    raw: list[str] = []
    name, data = None, None
    deadline = time.monotonic() + STREAM_TIMEOUT_S
    with client.stream("GET", path, timeout=httpx.Timeout(30.0)) as response:
        assert response.status_code == 200, response.read()
        for line in response.iter_lines():
            raw.append(line)
            assert time.monotonic() < deadline, "the stream did not end"
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
            elif line == "" and name is not None:
                events.append((name, data or {}, datetime.now(UTC)))
                name, data = None, None
    return events, "\n".join(raw)


def test_a_viewer_follows_status_and_transcript_through_andai_core(
    stack: dict[str, str],
    admin_url: str,
    engines: tuple[Engine, Engine],
    core_url: str,
    gateway: httpx.Client,
) -> None:
    _, superuser = engines
    link = zoom_link("emit-n-segments")
    with (
        live_org(stack, admin_url, engines, core_url, "a") as org,
        live_org(stack, admin_url, engines, core_url, "b") as other,
    ):
        joined = org.client.post("/api/v1/meetings/join", json={"join_url": link, "language": "en"})
        assert joined.status_code == 201, joined.text
        meeting_id = joined.json()["id"]
        path = f"/api/v1/meetings/{meeting_id}/live"

        refused = other.client.get(path)
        events, raw = read_stream(org.client, path)

        assert (refused.status_code, refused.json()["code"]) == (404, "no_such_meeting")
        assert org.key not in raw
        statuses = [(d["status"], at) for name, d, at in events if name == "status"]
        errors = [d for name, d, _ in events if name == "error"]
        assert errors == [], errors
        assert statuses and statuses[-1][0] == "done", statuses

        # Status: each one after the first (andai's own row) within 5 s of the platform's.
        with superuser.connect() as conn:
            platform_id = conn.execute(
                text("SELECT platform_meeting_id FROM meetings WHERE id = :m"), {"m": meeting_id}
            ).scalar_one()
        platform = gateway.get(f"/meetings/{platform_id}", headers={"X-API-Key": org.key}).json()
        transitions = platform_transitions(platform)
        assert transitions, "the platform meeting has no status_transition history"
        status_lags = {}
        for status, arrived in statuses[1:]:
            matching = [at for to, at in transitions if _andai_status_of(to, platform) == status]
            assert matching, (status, transitions)
            entered = matching[-1] if status == "done" else matching[0]
            lag = (arrived - entered).total_seconds()
            status_lags[status] = round(lag, 2)
            assert lag <= LATENCY_BUDGET_S, (status, lag, transitions)

        # Transcript: at least 12 segments with speaker and timestamps.
        segments: dict[str, tuple[dict, datetime]] = {}
        for name, data, arrived in events:
            if name == "segment":
                segments[data["id"]] = (data, arrived)
        assert len(segments) >= MIN_SEGMENTS, len(segments)
        for data, _ in segments.values():
            assert data["speaker"] and data["text"], data
            assert data["start"] is not None and data["end"] is not None, data
            assert data["absolute_start"] and data["absolute_end"], data

        polled = org.client.get(f"/api/v1/meetings/{meeting_id}/transcript")
        assert polled.status_code == 200, polled.text
        assert {s["id"] for s in polled.json()["segments"]} == set(segments)

        lags = [
            round((arrived - _parse(data["absolute_end"])).total_seconds(), 2)
            for data, arrived in sorted(segments.values(), key=lambda s: s[0]["start"])
        ]
        print(
            f"\nstatuses {[s for s, _ in statuses]} status_lags_s={status_lags}"
            f"\nsegments {len(segments)} arrival_minus_absolute_end_s={lags}"
        )
