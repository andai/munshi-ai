"""Canaries, alert mail and `/metrics` end to end against a running mock-bot stack (marked `live`).

Needs a test stack (never a deployment) brought up with `make -C andai up PROJECT=<test project>`
from the env file named by `ENV_FILE`, with:

* `BROWSER_IMAGE=mock-bot:dev` (the mock bot; `mock=<scenario>` on a link picks its behaviour);
* `ANDAI_ALERT_EMAIL` set and `ANDAI_CANARY_INTERVAL_S=20` (the container's alert evaluator then
  ticks every 5 s and mails through the stack's Mailpit);
* optionally `ANDAI_CANARY_ZOOM_URL` set to a Zoom-shaped link with `mock=normal`, so the
  container's own canary loop runs too (its runs show in `/metrics`).

The test runs the canary scheduler in this process against the stack's andai database, platform
gateway and mock bot, as the canary org (created with `canary-setup` if missing). It sends Google
Meet canaries to `mock=reject` until the container mails the alert, sends one more (no second
mail), then switches the link to `mock=normal` and waits for the recovery mail. andai-core's
reconciler moves the canary meetings on; the container's evaluator opens and closes the incident.
It checks Mailpit (one alert, one recovery, to `ANDAI_ALERT_EMAIL` only, no meeting link in them)
and the container's `/metrics` (the canary counts and streak).

    ENV_FILE=/abs/env ANDAI_REQUIRE_DOCKER=1 uv run --directory andai pytest -q -s -m live \\
        services/worker/tests/test_worker_canary_live.py
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
import re
import string
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text

from andai_core.crypto import Keyring
from andai_core.db.session import create_app_engine, transaction
from andai_core.meetings.service import Meetings, canary_actor
from andai_core.platform.admin_client import HttpPlatformAdmin
from andai_core.platform.gateway_client import HttpPlatformGateway
from andai_worker.canary import CanaryRunner, tick_seconds
from andai_worker.settings import WorkerSettings

pytestmark = pytest.mark.live

INTERVAL_S = 20
MEET = "https://meet.google.com/{code}?mock={scenario}"
PHASE_TIMEOUT_S = 150.0
OPEN_SUBJECT = "[munshi.ai] Bot joins failing on Google Meet"
RECOVERY_SUBJECT = "[munshi.ai] Recovered: bot joins on Google Meet"


def _env() -> dict[str, str]:
    name = os.environ.get("ENV_FILE")
    if not name:
        pytest.skip("live test: set ENV_FILE to the running test stack's env file")
    values: dict[str, str] = {}
    for line in Path(name).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    required = ("ADMIN_TOKEN", "ANDAI_DB_PASSWORD_APP", "ANDAI_KEK", "ANDAI_ALERT_EMAIL")
    missing = [k for k in required if not values.get(k)]
    if missing:
        pytest.fail(f"{name} lacks {', '.join(missing)}", pytrace=False)
    if values.get("ANDAI_CANARY_INTERVAL_S") != str(INTERVAL_S):
        pytest.fail(f"{name}: set ANDAI_CANARY_INTERVAL_S={INTERVAL_S}", pytrace=False)
    if "mock" not in values.get("BROWSER_IMAGE", ""):
        pytest.fail(f"{name}: BROWSER_IMAGE must be the mock bot", pytrace=False)
    return values


@pytest.fixture(scope="module")
def stack() -> dict[str, str]:
    return _env()


@pytest.fixture(scope="module")
def engine(stack: dict[str, str]) -> Iterator[Engine]:
    port = stack.get("POSTGRES_HOST_PORT") or 5458
    engine = create_app_engine(
        f"postgresql+psycopg://andai_app:{stack['ANDAI_DB_PASSWORD_APP']}@127.0.0.1:{port}/andai"
    )
    yield engine
    engine.dispose()


def _url(stack: dict[str, str], key: str, default: int) -> str:
    return f"http://127.0.0.1:{stack.get(key) or default}"


def _mails(api: str, to: str, subject: str, since: datetime) -> list[dict]:
    """Mailpit messages to `to` with `subject` received after `since`, with their text bodies."""
    with httpx.Client(base_url=api, timeout=10) as client:
        listing = client.get("/api/v1/messages", params={"limit": 500}).json()
        found = []
        for m in listing.get("messages", []):
            created = datetime.fromisoformat(m["Created"].replace("Z", "+00:00"))
            recipients = [a.get("Address", "").lower() for a in m.get("To", [])]
            if m.get("Subject") == subject and created >= since and to.lower() in recipients:
                found.append(client.get(f"/api/v1/message/{m['ID']}").json())
    return found


def _decided(engine: Engine, since: datetime) -> list[tuple[str, str | None]]:
    with transaction(engine) as tx:
        return [
            (outcome, reason)
            for outcome, reason in tx.execute(
                text(
                    "SELECT outcome, reason FROM canary_runs WHERE platform = 'google_meet'"
                    " AND outcome IS NOT NULL AND started_at >= :s ORDER BY started_at"
                ),
                {"s": since},
            ).all()
        ]


async def _run_until(runner: CanaryRunner, done, timeout: float, every: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await runner.tick()
        if done():
            return
        await asyncio.sleep(every)
    raise AssertionError("timed out")


def test_two_failed_canaries_mail_one_alert_and_a_success_mails_the_recovery(
    stack: dict[str, str], engine: Engine
) -> None:
    keyring = Keyring(base64.b64decode(stack["ANDAI_KEK"]))
    admin = HttpPlatformAdmin(
        _url(stack, "ADMIN_API_PORT", 18057), SecretStr(stack["ADMIN_TOKEN"])
    )
    if canary_actor(engine) is None:
        from andai_core.__main__ import main

        assert main(["canary-setup"], engine=engine, admin=admin, keyring=keyring) == 0
    with transaction(engine) as tx:
        open_before = tx.execute(
            text(
                "SELECT count(*) FROM alert_state WHERE kind = 'canary_failures'"
                " AND platform = 'google_meet' AND open"
            )
        ).scalar_one()
    assert open_before == 0, "a Google Meet canary incident is already open on this stack"

    settings = WorkerSettings(ANDAI_ENV="dev")
    meetings = Meetings(
        engine,
        keyring,
        HttpPlatformGateway(_url(stack, "API_GATEWAY_HOST_PORT", 18056)),
        settings,
        lambda: datetime.now(UTC),
    )
    # A Meet-shaped code unique to the run, so no earlier meeting of the stack shares its link.
    code = "-".join(
        "".join(random.choice(string.ascii_lowercase) for _ in range(n)) for n in (3, 4, 3)
    )
    mailpit = _url(stack, "MAILPIT_UI_PORT", 8025)
    to = stack["ANDAI_ALERT_EMAIL"]
    started = datetime.now(UTC)
    every = tick_seconds(INTERVAL_S)

    def runner(scenario: str) -> CanaryRunner:
        link = MEET.format(code=code, scenario=scenario)
        return CanaryRunner(
            engine, meetings, {"google_meet": link}, interval_s=INTERVAL_S,
            clock=lambda: datetime.now(UTC), hold_s=5,
        )

    # Phase 1: rejects until the container mails the alert.
    rejecting = runner("reject")
    t0 = time.monotonic()
    asyncio.run(_run_until(
        rejecting, lambda: len(_mails(mailpit, to, OPEN_SUBJECT, started)) >= 1,
        PHASE_TIMEOUT_S, every,
    ))
    alert_after = time.monotonic() - t0
    decided = _decided(engine, started)
    print(f"\nalert mail after {alert_after:.1f} s; canaries so far: {decided}")
    assert decided[:2] == [("failure", "admission_denied")] * 2
    assert all(outcome == "failure" for outcome, _ in decided)

    # Phase 2: one more reject; the incident is open already, so no second mail.
    asyncio.run(_run_until(
        rejecting, lambda: len(_decided(engine, started)) >= len(decided) + 1,
        PHASE_TIMEOUT_S, every,
    ))
    time.sleep(3 * every)  # a few evaluator ticks
    assert len(_mails(mailpit, to, OPEN_SUBJECT, started)) == 1

    # Phase 3: the link admits again; the next canary succeeds and the recovery mail follows.
    t1 = time.monotonic()
    asyncio.run(_run_until(
        runner("normal"), lambda: len(_mails(mailpit, to, RECOVERY_SUBJECT, started)) >= 1,
        PHASE_TIMEOUT_S, every,
    ))
    print(f"recovery mail after {time.monotonic() - t1:.1f} s; canaries: {_decided(engine, started)}")
    time.sleep(2 * every)

    [opened] = _mails(mailpit, to, OPEN_SUBJECT, started)
    [recovered] = _mails(mailpit, to, RECOVERY_SUBJECT, started)
    assert _decided(engine, started)[-1] == ("success", None)
    for mail in (opened, recovered):
        assert [a["Address"].lower() for a in mail["To"]] == [to.lower()]
        assert not mail.get("Cc") and not mail.get("Bcc")
        assert code not in mail["Text"] and "mock=" not in mail["Text"]
        assert "http" not in mail["Text"].replace("/metrics", "")
    assert "failure - admission_denied" in opened["Text"]

    with httpx.Client(timeout=10) as client:
        scrape = client.get(f"{_url(stack, 'ANDAI_WORKER_PORT', 18301)}/metrics")
    assert scrape.status_code == 200
    body = scrape.text
    print("\n".join(line for line in body.splitlines() if not line.startswith("#")))
    excluded = re.search(
        r'munshi_bot_joins_total\{outcome="excluded",platform="google_meet",'
        r'reason="admission_denied",source="canary"\} (\S+)', body,
    )
    success = re.search(
        r'munshi_bot_joins_total\{outcome="success",platform="google_meet",reason="",'
        r'source="canary"\} (\S+)', body,
    )
    assert excluded and float(excluded.group(1)) >= 3
    assert success and float(success.group(1)) >= 1
    assert 'munshi_canary_consecutive_failures{platform="google_meet"} 0.0' in body
    assert 'munshi_canary_last_run_timestamp_seconds{outcome="failure",platform="google_meet"}' in body
    assert code not in body and "http" not in body
