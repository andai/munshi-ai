"""Building a data export (`andai_core.dpdp.builder`) on Postgres with the stub platform: the bundle
holds exactly the meetings the person owns (the erasure set), every segment and minutes version,
people by name only, the consent record and each recording's size and SHA-256 from one streamed
read; the JSON part is sealed under the request's own associated data; a part over the cap fails
`too_large`, a person who left fails `not_a_member`, and platform errors retry then fail."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from andai_core.crypto import DecryptionError
from andai_core.db.session import transaction
from andai_core.dpdp import builder, bundle
from conftest import (
    EXPORT_LINES,
    ExportWorld,
    export_request,
    export_world,
    make_settings,
)


@pytest.fixture
def world(app_engine: Engine) -> ExportWorld:
    return export_world(app_engine)


def _build(world: ExportWorld, request_id: uuid.UUID, **settings: str) -> str | None:
    s = world.harness.services
    return asyncio.run(
        builder.run(
            world.engine, s.keyring, s.gateway, world.org_id, request_id,
            settings=make_settings(**settings), clock=s.clock,
        )
    )


def _row(superuser_engine: Engine, request_id: uuid.UUID) -> dict:
    with superuser_engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT * FROM export_requests WHERE id = :r"), {"r": request_id}
            ).mappings().one()
        )


def _files(world: ExportWorld, superuser_engine: Engine, request_id: uuid.UUID) -> dict:
    row = _row(superuser_engine, request_id)
    return builder.open_part(
        world.harness.services.keyring, world.org_id, request_id,
        row["bundle_ciphertext"], row["key_id"],
    )


def _audit(superuser_engine: Engine, org_id: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text("SELECT action, actor_user_id, target FROM audit_events WHERE org_id = :o"
                     " AND action LIKE 'user.export.%' ORDER BY at"),
                {"o": org_id},
            )
        ]


def test_the_bundle_holds_the_owned_meetings_and_nothing_else(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    request_id = export_request(world.engine, world.org_id, world.subject.user_id)
    calls_before = len(world.harness.gateway.calls)

    assert _build(world, request_id) == "ready"

    files = _files(world, superuser_engine, request_id)
    assert list(files)[-1] == "manifest.json"
    meetings = {
        uuid.UUID(path.split("/")[1].removesuffix(".json")): json.loads(data)
        for path, data in files.items()
        if path.startswith("meetings/")
    }
    assert set(meetings) == world.owned  # not the received one, not the deleted one
    for data in meetings.values():
        bundle.MeetingExport.model_validate(data)

    recorded = meetings[world.recorded]
    assert [s["textRaw"] for s in recorded["transcript"]] == [t for _, t in EXPORT_LINES]
    assert [s["speaker"] for s in recorded["transcript"]] == [n for n, _ in EXPORT_LINES]
    assert recorded["transcript"][0]["textCanonical"].isascii()  # the Roman form of Devanagari
    assert [v["version"] for v in recorded["minutes"]] == [1, 2]
    assert recorded["minutes"][1]["body"]["summary"] == "Second version."
    names = {g["type"]: g["name"] for g in recorded["sharedWith"]}
    assert len(recorded["sharedWith"]) == 2
    assert names["user"] == "Rohan Mehta" and names["team"].startswith("Finance")
    assert recorded["attendees"] == ["Asha Verma", "Rohan Mehta"]
    assert recorded["consent"]["disclosureChannel"] == "chat"
    assert recorded["language"] == "hinglish"

    by_size = sorted(recorded["recordings"], key=lambda r: r["bytes"])
    expected = sorted(world.audio.values(), key=len)
    assert [(r["bytes"], r["sha256"]) for r in by_size] == [
        (len(d), hashlib.sha256(d).hexdigest()) for d in expected
    ]
    assert all(r["mediaType"] == "audio/wav" for r in by_size)
    assert all(r["file"].startswith(f"recordings/{world.recorded}/") for r in by_size)
    # Each recording was streamed exactly once: master, then the raw bytes.
    raws = [c for c in world.harness.gateway.calls[calls_before:] if c[1].endswith("/raw")]
    assert len(raws) == 3  # two recordings of `recorded`, one attempt at `masterless`
    assert meetings[world.masterless]["recordings"] == []
    assert meetings[world.failed]["recordings"] == [] and meetings[world.failed]["transcript"] == []

    manifest = bundle.Manifest.model_validate(json.loads(files["manifest.json"]))
    assert manifest.subject.user_id == world.subject.user_id
    assert manifest.org.org_id == world.org_id
    for entry in manifest.files:
        if entry.path in files:
            assert entry.sha256 == hashlib.sha256(files[entry.path]).hexdigest()
            assert entry.bytes == len(files[entry.path])
    assert {e.path for e in manifest.files} == (set(files) - {"manifest.json"}) | {
        r["file"] for r in recorded["recordings"]
    }
    assert [(u.meeting_id, u.reason) for u in manifest.recordings_unavailable] == [
        (world.masterless, "no_master")
    ]

    account = json.loads(files["account.json"])
    assert account["email"] == world.subject.email
    assert account["displayName"] == "Asha Verma"
    assert account["membership"]["role"] == "member"
    assert [t["lead"] for t in account["membership"]["teams"]] == [False]
    assert account["processing"]["note"]

    row = _row(superuser_engine, request_id)
    assert row["state"] == "ready" and row["error"] is None
    assert row["expires_at"] - row["ready_at"] == timedelta(days=7)
    assert row["bundle_bytes"] == sum(len(d) for d in files.values()) + sum(
        len(d) for d in world.audio.values()
    )
    assert sorted(r["bytes"] for r in row["recordings"]) == sorted(
        len(d) for d in world.audio.values()
    )
    assert _audit(superuser_engine, world.org_id) == [
        ("user.export.ready", world.subject.user_id, f"user:{world.subject.user_id}")
    ]


def test_no_secret_or_other_persons_address_is_in_the_bundle(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    request_id = export_request(world.engine, world.org_id, world.subject.user_id)
    assert _build(world, request_id) == "ready"
    everything = "".join(d.decode() for d in _files(world, superuser_engine, request_id).values())

    for other in (world.other, world.owner, world.admin):
        assert other.email not in everything
    assert "$argon2" not in everything
    assert world.key not in everything
    assert "not the subject's meeting" not in everything


def test_the_json_part_is_sealed_under_the_requests_own_associated_data(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    request_id = export_request(world.engine, world.org_id, world.subject.user_id)
    assert _build(world, request_id) == "ready"
    row = _row(superuser_engine, request_id)
    keyring = world.harness.services.keyring

    assert row["key_id"] == keyring.key_id
    assert "Asha Verma" not in row["bundle_ciphertext"]
    for wrong in (uuid.uuid4(), world.org_id):
        with pytest.raises(DecryptionError):
            builder.open_part(keyring, world.org_id, wrong, row["bundle_ciphertext"], row["key_id"])
    with pytest.raises(DecryptionError):
        builder.open_part(
            keyring, uuid.uuid4(), request_id, row["bundle_ciphertext"], row["key_id"]
        )


def test_an_admins_request_is_built_the_same_way(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    request_id = export_request(
        world.engine, world.org_id, world.subject.user_id, world.admin.user_id
    )
    assert _build(world, request_id) == "ready"
    files = _files(world, superuser_engine, request_id)
    assert json.loads(files["account.json"])["userId"] == str(world.subject.user_id)
    assert _audit(superuser_engine, world.org_id) == [
        ("user.export.ready", world.admin.user_id, f"user:{world.subject.user_id}")
    ]


def test_a_json_part_over_the_cap_fails_too_large(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    request_id = export_request(world.engine, world.org_id, world.subject.user_id)

    assert _build(world, request_id, ANDAI_EXPORT_MAX_BYTES="2048") == "failed"

    row = _row(superuser_engine, request_id)
    assert (row["state"], row["error"], row["bundle_ciphertext"]) == ("failed", "too_large", None)
    assert [a[0] for a in _audit(superuser_engine, world.org_id)] == ["user.export.failed"]


def test_a_person_who_left_the_org_fails_at_once(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    from andai_core.erasure.requests import remove_from_org

    request_id = export_request(
        world.engine, world.org_id, world.subject.user_id, world.admin.user_id
    )
    with transaction(world.engine, org_id=world.org_id) as tx:
        remove_from_org(tx, world.org_id, world.subject.user_id)

    assert _build(world, request_id) == "failed"
    row = _row(superuser_engine, request_id)
    assert (row["state"], row["error"], row["attempts"]) == ("failed", "not_a_member", 0)


def test_platform_errors_retry_with_backoff_then_fail(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    request_id = export_request(world.engine, world.org_id, world.subject.user_id)
    world.harness.gateway.fail["list recordings"] = 503

    for attempt in range(1, builder.MAX_ATTEMPTS):
        assert _build(world, request_id) == "running"
        row = _row(superuser_engine, request_id)
        assert (row["state"], row["attempts"]) == ("running", attempt)
        assert row["next_at"] - world.harness.clock() == builder.backoff(attempt)

    assert _build(world, request_id) == "failed"
    row = _row(superuser_engine, request_id)
    assert (row["state"], row["error"], row["attempts"]) == (
        "failed", "internal_error", builder.MAX_ATTEMPTS
    )
    assert [a[0] for a in _audit(superuser_engine, world.org_id)] == ["user.export.failed"]


def test_only_a_running_request_is_built(world: ExportWorld, superuser_engine: Engine) -> None:
    pending = export_request(world.engine, world.org_id, world.subject.user_id, state="pending")
    assert _build(world, pending) is None
    assert _row(superuser_engine, pending)["state"] == "pending"
    other_org = uuid.uuid4()
    s = world.harness.services
    assert asyncio.run(
        builder.run(world.engine, s.keyring, s.gateway, other_org, pending,
                    settings=make_settings(), clock=s.clock)
    ) is None


def test_the_owned_set_is_the_erasure_registrys() -> None:
    from andai_core.db.models import Meeting

    assert builder.owner_column() is Meeting.requested_by


def test_a_meeting_still_going_on_lists_its_recording_as_in_progress(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(
            text("UPDATE meetings SET status = 'recording' WHERE id = :m"), {"m": world.recorded}
        )
    request_id = export_request(world.engine, world.org_id, world.subject.user_id)
    calls_before = len(world.harness.gateway.calls)

    assert _build(world, request_id) == "ready"

    manifest = json.loads(_files(world, superuser_engine, request_id)["manifest.json"])
    assert {(u["meetingId"], u["reason"]) for u in manifest["recordingsUnavailable"]} == {
        (str(world.recorded), "meeting_in_progress"), (str(world.masterless), "no_master")
    }
    called = [path for _, path in world.harness.gateway.calls[calls_before:]]
    assert not any(f"/recordings/{rid}/" in path for rid in world.audio for path in called)
