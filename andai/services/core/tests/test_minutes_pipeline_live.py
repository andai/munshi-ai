"""The minutes pipeline end to end against a running stack with the mock bot and the fake LLM
(marked `live`).

Needs an isolated andai compose stack up with `BROWSER_IMAGE=mock-bot:dev`, `COMPOSE_PROFILES`
including `e2e` (the `fake-llm` service) and `ANDAI_LLM_BASE_URL=http://fake-llm:8000` in the env
file named by `ENV_FILE` (skips without it; fails when it is set but the stack does not answer).
The stack is the compose project named by the file's `COMPOSE_PROJECT_NAME`. It creates an org
through `create_org` (real admin-api), signs its owner in through andai-core, and:

* **Ten meetings in a row.** Each pastes a Zoom-shaped link with `mock=hindi-lines` (six Devanagari
  and Roman Hinglish lines), dispatch language cycling en / hi / hinglish, and waits for the
  meeting to be `done`, its transcript `stored` and its minutes `stored`. Each must have exactly one
  minutes version in the dispatch language, stored within 60 s of the platform's end time, and
  the platform's `meeting.completed` webhook must have reached andai-core (one
  `platform_webhook_events` row for its platform meeting id).
* **Restart.** A `mock=paced` meeting (12 s in the lobby, then 8 lines 1.5 s apart): once andai
  says `recording`, the andai-core and andai-worker containers are killed; when the platform says
  the meeting is over they are started again, and the minutes still appear (the claim loop; the
  webhook delivered while andai-core was down is retried by the platform later).
* **No flows.** No flows reaction exists for any of these meetings' platform ids (flows' own
  `reaction` table, `done-<id>` / `live-<id>`), and Mailpit received no message during the run.

Timings per meeting (end → transcript stored → minutes stored, webhook arrival) are printed and,
with `MINUTES_LIVE_LOG=/abs/file`, written there as JSON. No key, host, id or content is printed.
The org, its users and tokens are removed afterwards.

    ENV_FILE=$PWD/.work/.local/env.0602 \\
        MINUTES_LIVE_LOG=$PWD/.work/phases/06-mom-pipeline/live/pipeline-0602.json \\
        ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q -s -m live \\
        services/core/tests/test_minutes_pipeline_live.py
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, create_engine, text

from conftest import zoom_link
from test_live_relay_live import live_org
from test_meetings_live import LiveOrg, core_url, gateway  # noqa: F401
from test_principals_live import admin_url, engines, stack  # noqa: F401

pytestmark = pytest.mark.live

RUNS = 10
LANGUAGES = ("en", "hi", "hinglish")
MOM_BUDGET_S = 60.0
WAIT_S = 240.0
POLL_S = 0.5
REPORT: dict = {"runs": [], "restart": None, "flows": None}


def _row(superuser: Engine, meeting_id: str) -> dict | None:
    with superuser.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, platform_meeting_id, ended_at, transcript_state, transcript_at,"
                " mom_state, mom_at, mom_error, mom_attempts, recap_state"
                " FROM meetings WHERE id = :m"
            ),
            {"m": meeting_id},
        ).mappings().first()
    return None if row is None else dict(row)


def _versions(superuser: Engine, meeting_id: str) -> list[tuple[int, str, str]]:
    with superuser.connect() as conn:
        return [
            (r.version, r.output_language, r.template_id)
            for r in conn.execute(
                text(
                    "SELECT version, output_language, template_id FROM mom_documents"
                    " WHERE meeting_id = :m ORDER BY version"
                ),
                {"m": meeting_id},
            )
        ]


def _webhooks(superuser: Engine, platform_id: int) -> list[dict]:
    with superuser.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT event_id, event_type, received_at FROM platform_webhook_events"
                    " WHERE platform_meeting_id = :p ORDER BY received_at"
                ),
                {"p": platform_id},
            ).mappings()
        ]


def _wait_minutes(superuser: Engine, meeting_id: str, timeout: float = WAIT_S) -> dict:
    deadline = time.monotonic() + timeout
    row: dict | None = None
    while time.monotonic() < deadline:
        row = _row(superuser, meeting_id)
        if row and row["mom_state"] in ("stored", "failed", "skipped"):
            break
        time.sleep(POLL_S)
    assert row is not None
    assert (row["status"], row["transcript_state"], row["mom_state"]) == (
        "done", "stored", "stored"
    ), {k: row[k] for k in ("status", "transcript_state", "mom_state", "mom_error")}
    return row


def _wait_webhook(superuser: Engine, platform_id: int, timeout: float = 30.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _webhooks(superuser, platform_id)
        if found:
            return found
        time.sleep(POLL_S)
    return []


def _seconds(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds(), 2)


def _container(stack: dict[str, str], service: str) -> str:
    project = stack.get("COMPOSE_PROJECT_NAME") or "andai-meet"
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}",
         "--filter", f"label=com.docker.compose.service={service}"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    if not out:
        pytest.fail(f"no {service} container in compose project {project}", pytrace=False)
    return out[0]


def _running(container: str) -> bool:
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return out == "true"


def _healthy(url: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    pytest.fail(f"{url.rsplit('/', 1)[0].rsplit(':', 1)[-1]} did not come back", pytrace=False)


@pytest.fixture(scope="module", autouse=True)
def reachable(stack: dict[str, str], core_url: str) -> None:
    """ENV_FILE is set: an unreachable stack is a failure, not a skip."""
    try:
        response = httpx.get(f"{core_url}/healthz", timeout=5)
    except httpx.HTTPError as exc:
        pytest.fail(f"andai-core does not answer: {type(exc).__name__}", pytrace=False)
    assert response.status_code == 200
    if "e2e" not in (stack.get("COMPOSE_PROFILES") or "").split(","):
        pytest.fail("the env file must enable the e2e profile (fake-llm)", pytrace=False)
    if not (stack.get("ANDAI_LLM_BASE_URL") or "").strip():
        pytest.fail("the env file names no ANDAI_LLM_BASE_URL", pytrace=False)


@pytest.fixture(scope="module")
def started_at() -> datetime:
    return datetime.now(UTC)


def _write_report() -> None:
    print("\n[minutes-live] " + json.dumps(REPORT, default=str))
    log = os.environ.get("MINUTES_LIVE_LOG")
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text(json.dumps(REPORT, indent=2, default=str) + "\n")


def _join(org: LiveOrg, scenario: str, language: str) -> str:
    joined = org.client.post(
        "/api/v1/meetings/join", json={"join_url": zoom_link(scenario), "language": language}
    )
    assert joined.status_code == 201, joined.text
    return joined.json()["id"]


def test_ten_meetings_in_a_row_each_get_one_set_of_minutes_within_60_s(
    stack: dict[str, str],
    admin_url: str,
    engines: tuple[Engine, Engine],
    core_url: str,
    started_at: datetime,
) -> None:
    _, superuser = engines
    REPORT["project"] = stack.get("COMPOSE_PROJECT_NAME")
    REPORT["at"] = started_at.isoformat(timespec="seconds")
    REPORT["platform_ids"] = []
    with live_org(stack, admin_url, engines, core_url, "minutes") as org:
        for run in range(RUNS):
            language = LANGUAGES[run % len(LANGUAGES)]
            meeting_id = _join(org, "hindi-lines", language)
            row = _wait_minutes(superuser, meeting_id)
            hooks = _wait_webhook(superuser, row["platform_meeting_id"])
            versions = _versions(superuser, meeting_id)
            REPORT["platform_ids"].append(row["platform_meeting_id"])
            entry = {
                "run": run + 1,
                "language": language,
                "end_to_transcript_s": _seconds(row["transcript_at"], row["ended_at"]),
                "transcript_to_mom_s": _seconds(row["mom_at"], row["transcript_at"]),
                "end_to_mom_s": _seconds(row["mom_at"], row["ended_at"]),
                "versions": len(versions),
                "recap_state": row["recap_state"],
                "webhooks": len(hooks),
                "webhook_after_end_s": _seconds(hooks[0]["received_at"], row["ended_at"])
                if hooks else None,
            }
            REPORT["runs"].append(entry)
            print(f"\n[minutes-live] run {run + 1}: " + json.dumps(entry))

            assert versions == [(1, language, "standard")]
            assert row["recap_state"] == "pending"
            assert entry["end_to_mom_s"] is not None
            assert entry["end_to_mom_s"] <= MOM_BUDGET_S, entry
            assert [h["event_type"] for h in hooks] == ["meeting.completed"], entry
    latencies = [r["end_to_mom_s"] for r in REPORT["runs"]]
    REPORT["end_to_mom_s"] = {
        "min": min(latencies),
        "median": statistics.median(latencies),
        "max": max(latencies),
    }
    _write_report()


def test_killing_andai_during_a_meeting_still_yields_its_minutes(
    stack: dict[str, str],
    admin_url: str,
    engines: tuple[Engine, Engine],
    core_url: str,
    gateway: httpx.Client,
) -> None:
    _, superuser = engines
    core = _container(stack, "andai-core")
    worker = _container(stack, "andai-worker")
    worker_url = f"http://127.0.0.1:{stack.get('ANDAI_WORKER_PORT') or 18301}/healthz"
    with live_org(stack, admin_url, engines, core_url, "restart") as org:
        meeting_id = _join(org, "paced", "hinglish")
        deadline = time.monotonic() + WAIT_S
        row = _row(superuser, meeting_id)
        while time.monotonic() < deadline and (row is None or row["status"] != "recording"):
            time.sleep(0.2)
            row = _row(superuser, meeting_id)
        assert row is not None and row["status"] == "recording", row
        platform_id = row["platform_meeting_id"]

        subprocess.run(["docker", "kill", core, worker], capture_output=True, check=True)
        killed_at = datetime.now(UTC)
        assert not _running(core) and not _running(worker)

        # The meeting ends on the platform while andai is down.
        status = None
        while time.monotonic() < deadline:
            status = gateway.get(
                f"/meetings/{platform_id}", headers={"X-API-Key": org.key}
            ).json().get("status")
            if status in ("completed", "failed"):
                break
            time.sleep(1)
        assert status == "completed", status
        down = _row(superuser, meeting_id)
        assert down is not None and down["status"] == "recording"  # nobody wrote it while down
        still_down = not _running(core) and not _running(worker)

        subprocess.run(["docker", "start", core, worker], capture_output=True, check=True)
        restarted_at = datetime.now(UTC)
        _healthy(f"{core_url}/healthz")
        _healthy(worker_url)
        row = _wait_minutes(superuser, meeting_id)
        versions = _versions(superuser, meeting_id)

        REPORT["restart"] = {
            "killed_while": "recording",
            "stayed_down_until_the_platform_ended_it": still_down,
            "down_s": _seconds(restarted_at, killed_at),
            "restart_to_mom_s": _seconds(row["mom_at"], restarted_at),
            "end_to_mom_s": _seconds(row["mom_at"], row["ended_at"]),
            "versions": len(versions),
            "webhooks_so_far": len(_webhooks(superuser, platform_id)),
        }
        REPORT.setdefault("platform_ids", []).append(platform_id)
        print("\n[minutes-live] restart: " + json.dumps(REPORT["restart"]))

        assert still_down
        assert versions == [(1, "hinglish", "standard")]
    _write_report()


def test_flows_neither_reacted_to_nor_mailed_about_these_meetings(
    stack: dict[str, str], started_at: datetime
) -> None:
    platform_ids = REPORT.get("platform_ids") or []
    assert platform_ids, "run the two tests above first (same module)"
    port = stack.get("POSTGRES_HOST_PORT") or 5458
    munshi = create_engine(
        f"postgresql+psycopg://{stack.get('DB_USER') or 'postgres'}:{stack.get('DB_PASSWORD', '')}"
        f"@127.0.0.1:{port}/{stack.get('DB_NAME') or 'munshi'}"
    )
    try:
        with munshi.connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('public.reaction')")).scalar()
            keys = [f"{kind}-{pid}" for pid in platform_ids for kind in ("done", "live")]
            reactions = (
                conn.execute(
                    text("SELECT count(*) FROM reaction WHERE source_event_id = ANY(:k)"),
                    {"k": keys},
                ).scalar_one()
                if exists
                else 0
            )
    finally:
        munshi.dispose()
    mailpit = f"http://127.0.0.1:{stack.get('MAILPIT_UI_PORT') or 8025}"
    messages = httpx.get(f"{mailpit}/api/v1/messages", params={"limit": 500}, timeout=10).json()
    new = [
        m for m in messages.get("messages", [])
        if datetime.fromisoformat(m["Created"].replace("Z", "+00:00")) >= started_at
    ]
    REPORT["flows"] = {
        "reaction_table": bool(exists),
        "reactions_for_these_meetings": reactions,
        "mail_during_the_run": len(new),
    }
    print("\n[minutes-live] flows: " + json.dumps(REPORT["flows"]))
    _write_report()

    assert reactions == 0
    assert new == []
