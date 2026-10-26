"""Two orgs' stored objects, against the running stack with the mock bot (marked `live`).

Needs the andai compose stack up with `BROWSER_IMAGE=mock-bot:dev` and the env file named by
`ENV_FILE` (skips without it). It creates two orgs through `create_org` (real admin-api), dispatches
one mock bot (`mock:normal`, recording on) per org through the gateway with each org's own key,
waits for `completed`, finalizes each recording's master, and then:

(a) lists the bucket the way meeting-api does (boto3 inside the meeting-api container, with the
    stack's storage credentials): every object written for the two orgs lies under its own
    `org/<org_id>/` prefix, each org has a chunk and a `master.*`, and nothing was written under the
    principals' unprefixed `recordings/<user>/` or `signal/<user>/` keys;
(b) with each org's key, calls every gateway route that reads recordings or deletes a meeting with
    the other org's ids: each answers 403/404 and leaks no id of the other org, and the other org's
    objects are all still there;
(c) D-37: both principals' bot-context reports `capture_signal: false` and their meetings left no
    `signal/` objects. A control meeting under an operator test user with capture left on shows
    whether the mock bot writes tapes at all (it does not today, so the tape check rests on the
    bot-context flag; the test prints which case it saw).

Each org then deletes its own meeting, which must remove every object it owns. Created rows,
tokens and objects are removed afterwards.

    ENV_FILE=/abs/env.smoke ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q -m live \\
        services/core/tests/test_storage_isolation_live.py
"""

from __future__ import annotations

import base64
import json
import random
import string
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text

from andai_core.crypto import Keyring
from andai_core.db.session import transaction
from andai_core.org.service import create_org
from andai_core.platform import principals
from andai_core.platform.admin_client import ADMIN_KEY_HEADER, HttpPlatformAdmin
from test_principals_live import _cleanup, admin_url, engines, stack  # noqa: F401

pytestmark = pytest.mark.live

TERMINAL = {"completed", "failed"}
LIST_SCRIPT = (
    "import os,sys,boto3\n"
    "ep=os.getenv('S3_ENDPOINT') or os.getenv('MINIO_ENDPOINT','')\n"
    "ep=ep if ep.startswith(('http://','https://')) else "
    "('https' if os.getenv('MINIO_SECURE','false').lower()=='true' else 'http')+'://'+ep\n"
    "c=boto3.client('s3',endpoint_url=ep,"
    "aws_access_key_id=os.getenv('S3_ACCESS_KEY') or os.getenv('MINIO_ACCESS_KEY'),"
    "aws_secret_access_key=os.getenv('S3_SECRET_KEY') or os.getenv('MINIO_SECRET_KEY'))\n"
    "b=os.getenv('MINIO_BUCKET') or os.getenv('RECORDING_BUCKET') or 'munshi'\n"
    "for page in c.get_paginator('list_objects_v2').paginate(Bucket=b):\n"
    "    for o in page.get('Contents',[]): print(o['Key'])\n"
)


def _meeting_api_container(stack: dict[str, str]) -> str:
    project = stack.get("COMPOSE_PROJECT_NAME") or "andai-meet"
    out = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
         "--filter", "label=com.docker.compose.service=meeting-api"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    if not out:
        pytest.fail(f"no running meeting-api container in compose project {project}", pytrace=False)
    return out[0]


def bucket_keys(container: str) -> set[str]:
    result = subprocess.run(
        ["docker", "exec", container, "python", "-c", LIST_SCRIPT],
        capture_output=True, text=True, check=False, timeout=120,
    )
    assert result.returncode == 0, f"bucket listing failed: {result.stderr[-400:]}"
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _native_id() -> str:
    letters = string.ascii_lowercase
    return "-".join("".join(random.choices(letters, k=n)) for n in (3, 4, 3))


@dataclass
class Party:
    """One API key holder: an org principal, or the control user."""

    name: str
    key: str = field(repr=False)
    platform_user_id: int
    prefix: str | None = None
    org_id: uuid.UUID | None = None
    meeting_id: int | None = None
    native_id: str | None = None
    recording_id: int | None = None
    media_file_id: int | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.key}


@pytest.fixture(scope="module")
def gateway(stack: dict[str, str]) -> Iterator[httpx.Client]:
    port = stack.get("API_GATEWAY_HOST_PORT") or 18056
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
        yield client


def _dispatch(gateway: httpx.Client, party: Party) -> None:
    party.native_id = _native_id()
    response = gateway.post(
        "/bots",
        headers=party.headers,
        json={
            "platform": "google_meet",
            "native_meeting_id": party.native_id,
            "bot_name": "mock:normal",
            "recording_enabled": True,
            "transcribe_enabled": False,
        },
    )
    assert response.status_code == 201, f"{party.name}: POST /bots {response.status_code}"
    party.meeting_id = int(response.json()["id"])


def _wait_completed(gateway: httpx.Client, party: Party, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = gateway.get(f"/meetings/{party.meeting_id}", headers=party.headers)
        if response.status_code == 200:
            body = response.json()
            if body.get("status") in TERMINAL:
                break
        time.sleep(2)
    assert body.get("status") == "completed", f"{party.name}: meeting ended as {body.get('status')}"
    return body


def _resolve_recording(gateway: httpx.Client, party: Party, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    recordings: list = []
    while time.monotonic() < deadline and not recordings:
        recordings = gateway.get(
            "/recordings", headers=party.headers, params={"meeting_id": party.meeting_id}
        ).json().get("recordings", [])
        if not recordings:
            time.sleep(2)
    assert recordings, f"{party.name}: no recording for meeting {party.meeting_id}"
    party.recording_id = int(recordings[0]["id"])
    detail = gateway.get(f"/recordings/{party.recording_id}", headers=party.headers)
    assert detail.status_code == 200
    party.media_file_id = int(detail.json()["media_files"][0]["id"])
    master = gateway.get(
        f"/recordings/{party.recording_id}/master", headers=party.headers, params={"type": "audio"}
    )
    assert master.status_code == 200, f"{party.name}: master {master.status_code}"


def _foreign_marks(party: Party) -> list[str]:
    """Strings that identify `party`'s meeting and storage unambiguously (its meeting id is a small
    integer that can occur in any body, so its native id stands for it)."""
    return [str(v) for v in (party.recording_id, party.native_id, party.prefix) if v]


def _probe_cross_org(gateway: httpx.Client, caller: Party, other: Party) -> list[str]:
    """Every gateway route that reads stored objects or deletes a meeting, with `other`'s ids."""
    rid, mf, mid, native = (
        other.recording_id, other.media_file_id, other.meeting_id, other.native_id
    )
    denied = [
        ("GET", f"/recordings/{rid}", None),
        ("GET", f"/recordings/{rid}/master", {"type": "audio"}),
        ("GET", f"/recordings/{rid}/media/{mf}/raw", None),
        ("GET", f"/recordings/{rid}/media/{mf}/download", None),
        ("DELETE", f"/recordings/{rid}", None),
        ("GET", f"/meetings/{mid}", None),
        ("DELETE", f"/meetings/{mid}", None),
        ("DELETE", f"/meetings/google_meet/{native}", None),
        ("GET", f"/transcripts/by-id/{mid}", None),
        ("GET", f"/transcripts/google_meet/{native}", None),
    ]
    seen = []
    for method, path, params in denied:
        response = gateway.request(method, path, headers=caller.headers, params=params)
        seen.append(f"{method} {path.replace(str(rid), '<rid>')} {response.status_code}")
        assert response.status_code in (403, 404), (
            f"{caller.name} → {method} {path}: {response.status_code}"
        )
        # A mark the caller itself sent (a native id in the path) may be echoed back; anything else
        # of the other org's in the body is a leak.
        sent = path + json.dumps(params or {})
        leaked = [m for m in _foreign_marks(other) if m not in sent and m in response.text]
        assert not leaked, f"{caller.name} → {method} {path} leaked {leaked}: {response.text[:200]}"
    # The caller's own listings never carry the other org's ids.
    for path, params in (("/recordings", {"meeting_id": mid}), ("/recordings", None),
                         ("/meetings", None)):
        response = gateway.get(path, headers=caller.headers, params=params)
        seen.append(f"GET {path} {response.status_code}")
        assert response.status_code == 200, f"{caller.name} → GET {path}: {response.status_code}"
        listed = json.dumps(response.json())
        assert not any(m in listed for m in _foreign_marks(other)), (
            f"{caller.name} → GET {path} listed {other.name}'s meeting or recording"
        )
    return seen


def test_two_orgs_store_under_their_own_prefix_and_cannot_reach_each_other(
    stack: dict[str, str],
    admin_url: str,
    engines: tuple[Engine, Engine],
    gateway: httpx.Client,
) -> None:
    app, superuser = engines
    admin_key = stack["ADMIN_TOKEN"]
    admin = HttpPlatformAdmin(admin_url, SecretStr(admin_key))
    keyring = Keyring(base64.b64decode(stack["ANDAI_KEK"]))
    admin_headers = {ADMIN_KEY_HEADER: admin_key}
    internal = {"X-Internal-Secret": stack["INTERNAL_API_SECRET"]}
    container = _meeting_api_container(stack)
    created: list[tuple[uuid.UUID, str, int]] = []
    control_user: int | None = None
    control: Party | None = None
    parties: list[Party] = []
    try:
        before = bucket_keys(container)
        for name in ("A", "B"):
            owner_email = f"live-iso-{uuid.uuid4().hex[:10]}@example.test"
            org = create_org(
                app,
                name=f"Live isolation {name} {uuid.uuid4().hex[:6]}",
                owner_email=owner_email,
                owner_display_name=None,
                admin=admin,
                keyring=keyring,
                now=datetime.now(UTC),
            )
            created.append((org.org_id, owner_email, org.platform_user_id))
            with transaction(app, org_id=org.org_id) as tx:
                key = principals.open_key(tx, keyring, org.org_id)
            assert key
            parties.append(Party(
                name=f"org {name}", key=key, platform_user_id=org.platform_user_id,
                prefix=f"org/{org.org_id}", org_id=org.org_id,
            ))
        a, b = parties

        with httpx.Client(base_url=admin_url, timeout=10) as client:
            for party in parties:
                context = client.get(
                    f"/internal/users/{party.platform_user_id}/bot-context", headers=internal
                ).json()
                assert context["storage_prefix"] == party.prefix
                assert context["capture_signal"] is False
            # The control: an operator test user with capture left as configured.
            control_email = f"live-iso-control-{uuid.uuid4().hex[:10]}@example.test"
            control_user = client.post(
                "/admin/users", headers=admin_headers, json={"email": control_email}
            ).json()["id"]
            control_key = client.post(
                f"/admin/users/{control_user}/tokens", headers=admin_headers,
                params={"scopes": "bot,tx"},
            ).json()["token"]
            control_capture = client.get(
                f"/internal/users/{control_user}/bot-context", headers=internal
            ).json()["capture_signal"]
        control = Party(name="control", key=control_key, platform_user_id=control_user)

        for party in (a, b, control):
            _dispatch(gateway, party)
        for party in (a, b, control):
            meeting = _wait_completed(gateway, party)
            assert (meeting.get("data") or {}).get("storage_prefix") == party.prefix
            _resolve_recording(gateway, party)

        # (a) the bucket layout.
        after = bucket_keys(container)
        new = after - before
        a_keys = {k for k in new if k.startswith(f"{a.prefix}/")}
        b_keys = {k for k in new if k.startswith(f"{b.prefix}/")}
        control_keys = {
            k for k in new
            if k.startswith((f"recordings/{control_user}/", f"signal/{control_user}/"))
        }
        assert new == a_keys | b_keys | control_keys, sorted(new - a_keys - b_keys - control_keys)
        for party, keys in ((a, a_keys), (b, b_keys)):
            own_root = (f"{party.prefix}/recordings/{party.platform_user_id}/",
                        f"{party.prefix}/signal/{party.platform_user_id}/")
            assert keys, f"{party.name}: nothing stored under {party.prefix}/"
            assert all(k.startswith(own_root) for k in keys), sorted(keys)
            assert any(k.rsplit("/", 1)[-1].startswith("master.") for k in keys)
            assert any(not k.rsplit("/", 1)[-1].startswith("master.") for k in keys)
            assert not any(
                k.startswith((f"recordings/{party.platform_user_id}/",
                              f"signal/{party.platform_user_id}/"))
                for k in after
            ), f"{party.name}: an unprefixed object exists for the principal"

        # (c) diagnostic tapes.
        assert not any("/signal/" in k for k in a_keys | b_keys)
        control_tapes = {k for k in control_keys if k.startswith("signal/")}
        print(
            f"\n[storage-isolation] control capture_signal={control_capture}; control tapes="
            f"{len(control_tapes)} → "
            + ("tape absence for the orgs is meaningful" if control_tapes else
               "the mock bot writes no tapes; the org tape check rests on the bot-context flag")
        )

        # (b) cross-org reads and deletes, both directions.
        for caller, other in ((a, b), (b, a)):
            seen = _probe_cross_org(gateway, caller, other)
            print(f"[storage-isolation] {caller.name} → {other.name}: " + "; ".join(seen))
        still = bucket_keys(container)
        assert a_keys | b_keys <= still, "a cross-org call removed objects"

        # Each org deletes its own meeting: every object it owns goes.
        for party, keys in ((a, a_keys), (b, b_keys)):
            response = gateway.delete(f"/meetings/{party.meeting_id}", headers=party.headers)
            assert response.status_code == 204, f"{party.name}: delete {response.status_code}"
        remaining = bucket_keys(container)
        assert not (a_keys | b_keys) & remaining, sorted((a_keys | b_keys) & remaining)
        print(f"[storage-isolation] org A {len(a_keys)} objects, org B {len(b_keys)} objects,"
              f" control {len(control_keys)}; all org objects removed by their owner's delete")
    finally:
        # Each holder deletes its own meeting's objects (a no-op once already deleted), then the
        # keys are revoked and the andai rows removed.
        for party in [*parties, *([control] if control else [])]:
            if party.meeting_id is not None:
                gateway.delete(f"/meetings/{party.meeting_id}", headers=party.headers)
        with httpx.Client(base_url=admin_url, timeout=10) as client:
            user_ids = [u for _, _, u in created] + ([control_user] if control_user else [])
            for user_id in user_ids:
                listed = client.get(f"/admin/users/{user_id}/tokens", headers=admin_headers)
                for token in listed.json() if listed.status_code == 200 else []:
                    client.delete(f"/admin/tokens/{token['id']}", headers=admin_headers)
        for org_id, owner_email, _ in created:
            _cleanup(superuser, org_id, owner_email)
