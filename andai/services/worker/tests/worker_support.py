"""Shared helpers for andai-worker tests.

andai-core's test fixtures and helpers (`services/core/tests/conftest.py`) are loaded under the
module name `andai_core_test_support` and re-exported here; `conftest.py` re-exports the fixtures so
pytest registers them for this directory. This directory is a package (`__init__.py`), so its
modules never take the top-level names `conftest` or `test_*` that andai-core's tests import by;
worker test modules import their helpers from here with a relative import.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine

_CORE_TESTS = Path(__file__).resolve().parents[2] / "core" / "tests"
_spec = importlib.util.spec_from_file_location("andai_core_test_support", _CORE_TESTS / "conftest.py")
assert _spec is not None and _spec.loader is not None
support = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = support
_spec.loader.exec_module(support)

# Fixtures, re-exported by conftest.py.
postgres = support.postgres
migrated = support.migrated
app_engine = support.app_engine
superuser_engine = support.superuser_engine
fresh_app_engine = support.fresh_app_engine
mailpit = support.mailpit

Clock = support.Clock
Harness = support.Harness
make_harness = support.make_harness
give_principal = support.give_principal
new_member = support.new_member
zoom_link = support.zoom_link
mailpit_messages = support.mailpit_messages


def canary_setup(harness, engine: Engine) -> uuid.UUID:
    """Run `python -m andai_core canary-setup` against `engine` with the harness's fake admin-api
    and keyring; returns the canary org's id."""
    from andai_core.__main__ import main
    from andai_core.meetings.service import canary_org_id

    assert main(
        ["canary-setup"],
        engine=engine,
        admin=harness.services.platform_admin,
        keyring=harness.services.keyring,
    ) == 0
    org_id = canary_org_id(engine)
    assert org_id is not None
    return org_id


def seed_meeting(
    engine: Engine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    platform: str = "zoom",
    source: str = "portal_link",
    status: str = "done",
    failure_reason: str | None = None,
    end_reason: str | None = None,
    started: bool | None = None,
    created_at: datetime | None = None,
    platform_meeting_id: int | None = None,
    native_meeting_id: str | None = None,
) -> uuid.UUID:
    """A meeting row in `org_id` written as the app role under that org's context."""
    from andai_core.db.models import Meeting
    from andai_core.db.session import transaction

    meeting_id = uuid.uuid4()
    started = status in ("recording", "processing", "done") if started is None else started
    with transaction(engine, org_id=org_id, user_id=user_id) as tx:
        tx.add(
            Meeting(
                id=meeting_id,
                org_id=org_id,
                requested_by=user_id,
                source=source,
                platform=platform,
                join_url=f"https://zoom.us/j/{meeting_id.int % 10**11:011d}?pwd=secret",
                join_url_key=f"zoom.us/j/{meeting_id.hex}",
                status=status,
                failure_reason=failure_reason,
                end_reason=end_reason,
                started_at=support.Clock()() if started else None,
                settings_snapshot={},
                platform_meeting_id=platform_meeting_id,
                native_meeting_id=native_meeting_id,
                **({} if created_at is None else {"created_at": created_at}),
            )
        )
    return meeting_id


HINGLISH_LINES = [
    {"segment_id": "s:1", "speaker": "Asha", "text": "कल की मीटिंग में deadline तय हुई",
     "start": 0.0, "end": 2.0, "completed": True, "language": "hi"},
    {"segment_id": "s:2", "speaker": "Vikram", "text": "ठीक है, मैं report भेज दूँगा",
     "start": 2.5, "end": 4.0, "completed": True, "language": "hi"},
]


def platform_meeting(
    harness, key: str, *, segments: list[dict] | None = None, status: str = "completed"
) -> tuple[int, str]:
    """A meeting of the org whose platform key is `key`, put straight into the harness's in-memory
    gateway (the stub platform) with its transcript; returns (platform meeting id, native id)."""
    from datetime import UTC

    gateway = harness.gateway
    [platform_user] = [
        t["user_id"] for t in harness.admin_api.tokens.values() if t["token"] == key
    ]
    native = f"{uuid.uuid4().int % 10**11:011d}"
    mid = next(gateway._ids)
    now = datetime.now(UTC).isoformat()
    gateway.meetings[mid] = {
        "id": mid,
        "user_id": platform_user,
        "platform": "zoom",
        "native_meeting_id": native,
        "constructed_meeting_url": f"https://zoom.us/j/{native}",
        "status": status,
        "bot_container_id": None,
        "start_time": now,
        "end_time": now,
        "completion_reason": None,
        "failure_stage": None,
        "data": {},
        "created_at": now,
        "updated_at": now,
    }
    gateway.owners[mid] = platform_user
    gateway.transcripts[mid] = [dict(s) for s in (HINGLISH_LINES if segments is None else segments)]
    return mid, native
