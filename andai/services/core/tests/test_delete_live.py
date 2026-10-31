"""Meeting deletion end to end against a running stack with the mock bot (marked `live`).

Needs an andai compose stack up with the mock bot as `BROWSER_IMAGE`, andai-core's reconciler and
andai-worker running, and the env file named by `ENV_FILE` (skips without it); the stack is the
compose project named by the file's `COMPOSE_PROJECT_NAME`. It creates an org through `create_org`
(real admin-api), signs its owner in through andai-core, pastes a Zoom-shaped link with a meeting
number unique to the run and `mock=normal` (the mock scenario that speaks and uploads a recording
chunk; `emit-n-segments` records nothing), and waits until the meeting is `done` and
andai-worker has stored its transcript (`transcript_state = stored`). It finalizes the recording's
master (`GET /recordings/{id}/master`), so both a chunk and the master are stored, and lists the
objects under the org's `org/<org_id>/recordings/` prefix (the org holds this one meeting) by
running `find` in the stack's storage container (versitygw's POSIX backend keeps every object as a
file), so no storage credential is needed or printed. Then:

* `DELETE /api/v1/meetings/{id}` answers 202, and at once the meeting page, its transcript and
  minutes answer 404, the list leaves it out and search finds nothing;
* every 5 s it reads andai's meeting row and the storage listing until both are gone, and asserts
  that took at most 15 minutes;
* afterwards the listing is empty, the platform's transcript read answers no segments, andai holds
  no row of the meeting (meeting, segments, shares, consent, minutes), and the `meeting.delete` and
  `meeting.purge` audit events are there.

It prints the purge time and the object counts (no keys, ids or host names), and with
`DELETE_LIVE_LOG=/abs/file` also writes them there. The org, its users and tokens are removed
afterwards.

    ENV_FILE=$PWD/.work/.local/env.0504 ANDAI_REQUIRE_DOCKER=1 uv run --directory andai \\
        pytest -q -s -m live services/core/tests/test_delete_live.py
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, text

from conftest import zoom_link
from test_live_relay_live import live_org
from test_meetings_live import LiveOrg, core_url, gateway  # noqa: F401
from test_principals_live import admin_url, engines, stack  # noqa: F401

pytestmark = pytest.mark.live

STORAGE_ROOT = "/srv/munshi-storage/data"
DONE_TIMEOUT_S = 180.0
PURGE_BUDGET_S = 15 * 60
POLL_S = 5.0
ANDAI_TABLES = (
    "transcript_segments", "mom_documents", "meeting_shares", "consent_records", "meetings"
)


def _storage_container(stack: dict[str, str]) -> str:
    project = stack.get("COMPOSE_PROJECT_NAME") or "andai-meet"
    out = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
         "--filter", "label=com.docker.compose.service=storage"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    if not out:
        pytest.fail(f"no running storage container in compose project {project}", pytrace=False)
    return out[0]


def stored_objects(container: str, bucket: str, prefix: str) -> list[str]:
    """Object keys under `prefix`, from the POSIX backend's files ([] once it is gone)."""
    root = f"{STORAGE_ROOT}/{bucket}"
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-c",
         f'[ -d "{root}/{prefix}" ] && find "{root}/{prefix}" -type f || true'],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, f"storage listing failed: {result.stderr[-300:]}"
    return sorted(
        line.removeprefix(f"{root}/") for line in result.stdout.splitlines() if line.strip()
    )


def _andai_rows(superuser: Engine, meeting_id: str) -> dict[str, int]:
    with superuser.connect() as conn:
        return {
            table: conn.execute(
                text(
                    f"SELECT count(*) FROM {table} WHERE "
                    + ("id" if table == "meetings" else "meeting_id")
                    + " = :m"
                ),
                {"m": meeting_id},
            ).scalar_one()
            for table in ANDAI_TABLES
        }


def _meeting_row(superuser: Engine, meeting_id: str) -> dict | None:
    with superuser.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, transcript_state, platform_meeting_id, deletion_state,"
                " deletion_attempts FROM meetings WHERE id = :m"
            ),
            {"m": meeting_id},
        ).mappings().first()
    return None if row is None else dict(row)


def _wait_stored(superuser: Engine, meeting_id: str) -> dict:
    deadline = time.monotonic() + DONE_TIMEOUT_S
    row: dict | None = None
    while time.monotonic() < deadline:
        row = _meeting_row(superuser, meeting_id)
        if row and row["status"] in ("done", "failed", "cancelled") and (
            row["transcript_state"] in ("stored", "failed")
        ):
            break
        time.sleep(1)
    assert row and row["status"] == "done", row
    assert row["transcript_state"] == "stored", row
    return row


def _finalize_master(gateway: httpx.Client, org: LiveOrg, platform_id: int) -> None:
    headers = {"X-API-Key": org.key}
    recordings: list = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not recordings:
        recordings = gateway.get(
            "/recordings", headers=headers, params={"meeting_id": platform_id}
        ).json().get("recordings", [])
        if not recordings:
            time.sleep(2)
    assert recordings, "the platform holds no recording for the meeting"
    master = gateway.get(
        f"/recordings/{recordings[0]['id']}/master", headers=headers, params={"type": "audio"}
    )
    assert master.status_code == 200, f"master {master.status_code}"


def _platform_segments(gateway: httpx.Client, org: LiveOrg, platform_id: int) -> tuple[int, int]:
    """(status, segment count) of the platform's transcript read with the org's key."""
    response = gateway.get(f"/transcripts/by-id/{platform_id}", headers={"X-API-Key": org.key})
    if response.status_code != 200:
        return response.status_code, 0
    return 200, len(response.json().get("segments") or [])


def _search_word(client: httpx.Client, meeting_id: str) -> str:
    """A word of the stored transcript that search finds this meeting by, before the delete."""
    segments = client.get(f"/api/v1/meetings/{meeting_id}/transcript").json()["segments"]
    for segment in segments:
        for word in segment["text"].split():
            word = "".join(c for c in word if c.isalnum())
            if len(word) < 3:
                continue
            found = client.get("/api/v1/search", params={"q": word})
            if found.status_code == 200 and meeting_id in {
                r["id"] for r in found.json()["results"]
            }:
                return word
    pytest.fail("search finds the meeting by none of its transcript words", pytrace=False)


def _audit(superuser: Engine, meeting_id: str) -> list[str]:
    with superuser.connect() as conn:
        return list(
            conn.execute(
                text("SELECT action FROM audit_events WHERE target = :t ORDER BY at"),
                {"t": f"meeting:{meeting_id}"},
            ).scalars()
        )


def test_a_deleted_meeting_is_purged_from_andai_and_the_platform_within_15_minutes(
    stack: dict[str, str],
    admin_url: str,
    engines: tuple[Engine, Engine],
    core_url: str,
    gateway: httpx.Client,
) -> None:
    _, superuser = engines
    container = _storage_container(stack)
    bucket = stack.get("MINIO_BUCKET") or "munshi"
    with live_org(stack, admin_url, engines, core_url, "delete") as org:
        prefix = f"org/{org.org_id}/recordings/"
        joined = org.client.post(
            "/api/v1/meetings/join",
            json={"join_url": zoom_link("normal"), "language": "en"},
        )
        assert joined.status_code == 201, joined.text
        meeting_id = joined.json()["id"]
        row = _wait_stored(superuser, meeting_id)
        platform_id = row["platform_meeting_id"]
        _finalize_master(gateway, org, platform_id)

        before = stored_objects(container, bucket, prefix)
        rows_before = _andai_rows(superuser, meeting_id)
        word = _search_word(org.client, meeting_id)
        assert before, f"nothing stored under {prefix}"
        assert any(k.rsplit("/", 1)[-1].startswith("master.") for k in before), before
        assert rows_before["meetings"] == 1 and rows_before["transcript_segments"] >= 1
        assert rows_before["consent_records"] == 1
        platform_before = _platform_segments(gateway, org, platform_id)[1]
        assert platform_before >= 1

        deleted = org.client.delete(f"/api/v1/meetings/{meeting_id}")
        requested = time.monotonic()
        assert deleted.status_code == 202, deleted.text

        # Hidden at once.
        for path in ("", "/transcript", "/mom", "/live"):
            hidden = org.client.get(f"/api/v1/meetings/{meeting_id}{path}")
            assert (hidden.status_code, hidden.json()["code"]) == (404, "no_such_meeting"), path
        listed = org.client.get("/api/v1/meetings", params={"limit": 50}).json()
        assert meeting_id not in json.dumps(listed)
        found = org.client.get("/api/v1/search", params={"q": word})
        assert found.status_code == 200 and found.json()["total"] == 0, found.text

        # Purged: andai's row and every stored object.
        polls = 0
        while True:
            polls += 1
            row = _meeting_row(superuser, meeting_id)
            left = stored_objects(container, bucket, prefix)
            if row is None and not left:
                break
            assert time.monotonic() - requested <= PURGE_BUDGET_S, (row, len(left))
            time.sleep(POLL_S)
        purge_s = round(time.monotonic() - requested, 1)

        after = stored_objects(container, bucket, prefix)
        rows_after = _andai_rows(superuser, meeting_id)
        platform_status, platform_segments = _platform_segments(gateway, org, platform_id)
        actions = _audit(superuser, meeting_id)

        assert after == []
        assert rows_after == dict.fromkeys(ANDAI_TABLES, 0)
        assert platform_status in (200, 404)
        assert platform_segments == 0
        assert "meeting.delete" in actions and "meeting.purge" in actions

        report = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "project": stack.get("COMPOSE_PROJECT_NAME"),
            "objects_before": len(before),
            "masters_before": sum(
                1 for k in before if k.rsplit("/", 1)[-1].startswith("master.")
            ),
            "andai_rows_before": rows_before,
            "purge_seconds": purge_s,
            "polls": polls,
            "objects_after": len(after),
            "andai_rows_after": rows_after,
            "platform_segments_before": platform_before,
            "platform_transcript_status": platform_status,
            "platform_segments_after": platform_segments,
            "audit_actions": actions,
        }
        print("\n[delete-live] " + json.dumps(report))
        log = os.environ.get("DELETE_LIVE_LOG")
        if log:
            Path(log).write_text(json.dumps(report, indent=2) + "\n")

