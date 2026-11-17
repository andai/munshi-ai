"""Asking for and downloading a copy of a person's data through the API (OPS-02): who may ask, the
one-open and three-a-day limits, the two listings, and the download — a zip whose manifest
checksums match every entry (recordings streamed from the stub platform), only for the person or the
admin who asked, 404 for anyone else, 409 before it is ready, 410 after it expired, 502 when a
recording changed — with every step audited; and one download building at a time per person (429
`export_download_busy`), the slot released after success, failure and cancellation."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
import uuid
import zipfile

import anyio.to_thread
import pytest
import redis
from sqlalchemy import Engine, text

from andai_core.auth.ratelimit import MemorySlots, ValkeySlots
from andai_core.dpdp import builder
from conftest import (
    ExportWorld,
    export_world,
    make_settings,
    new_member,
    sign_in,
)

MINE = "/api/v1/me/exports"


def _member_path(user_id: uuid.UUID) -> str:
    return f"/api/v1/org/users/{user_id}/exports"


def _download(export_id: str) -> str:
    return f"/api/v1/exports/{export_id}/download"


@pytest.fixture
def world(app_engine: Engine) -> ExportWorld:
    return export_world(app_engine)


def _build(world: ExportWorld, superuser_engine: Engine, export_id: str) -> str | None:
    """What andai-worker does: claim (set running) and build."""
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE export_requests SET state = 'running' WHERE id = :r"), {"r": export_id}
        )
    s = world.harness.services
    return asyncio.run(
        builder.run(world.engine, s.keyring, s.gateway, world.org_id, uuid.UUID(export_id),
                    settings=make_settings(), clock=s.clock)
    )


def _audit(superuser_engine: Engine, org_id: uuid.UUID) -> list[tuple]:
    with superuser_engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text("SELECT action, actor_user_id, target FROM audit_events WHERE org_id = :o"
                     " AND action LIKE 'user.export.%' ORDER BY at, action"),
                {"o": org_id},
            )
        ]


def _fail(superuser_engine: Engine, export_id: str) -> None:
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE export_requests SET state = 'failed', error = 'internal_error'"
                 " WHERE id = :r"),
            {"r": export_id},
        )


def test_a_member_asks_for_their_own_copy(world: ExportWorld, superuser_engine: Engine) -> None:
    client = sign_in(world.harness, world.subject)

    answer = client.post(MINE)

    assert answer.status_code == 202, answer.text
    body = answer.json()
    assert (body["state"], body["source"], body["subjectId"]) == (
        "pending", "self", str(world.subject.user_id)
    )
    assert body["canDownload"] is False and body["expiresAt"] is None
    assert _audit(superuser_engine, world.org_id) == [
        ("user.export.request", world.subject.user_id, f"user:{world.subject.user_id}")
    ]
    listed = client.get(MINE).json()["items"]
    assert [i["id"] for i in listed] == [body["id"]]


def test_one_open_request_and_three_a_day(world: ExportWorld, superuser_engine: Engine) -> None:
    client = sign_in(world.harness, world.subject)
    first = client.post(MINE).json()["id"]

    again = client.post(MINE)
    assert (again.status_code, again.json()["code"]) == (429, "export_in_progress")
    admin = sign_in(world.harness, world.admin)
    by_admin = admin.post(_member_path(world.subject.user_id))
    assert (by_admin.status_code, by_admin.json()["code"]) == (429, "export_in_progress")

    _fail(superuser_engine, first)
    for _ in range(2):
        made = client.post(MINE)
        assert made.status_code == 202
        _fail(superuser_engine, made.json()["id"])
    fourth = client.post(MINE)
    assert (fourth.status_code, fourth.json()["code"]) == (429, "export_rate_limited")

    world.harness.clock.advance(hours=24, seconds=1)
    client = sign_in(world.harness, world.subject)  # the old session idled out
    assert client.post(MINE).status_code == 202


@pytest.mark.parametrize("caller", ["owner", "admin"])
def test_owners_and_admins_ask_for_a_members_copy(
    world: ExportWorld, superuser_engine: Engine, caller: str
) -> None:
    person = getattr(world, caller)
    client = sign_in(world.harness, person)

    answer = client.post(_member_path(world.subject.user_id))

    assert answer.status_code == 202, answer.text
    body = answer.json()
    assert (body["source"], body["subjectName"]) == ("org_admin", "Asha Verma")
    listed = client.get("/api/v1/org/exports").json()
    assert [(i["id"], i["subjectName"]) for i in listed["items"]] == [(body["id"], "Asha Verma")]
    assert listed["next"] is None
    # the person sees it in their own list too
    mine = sign_in(world.harness, world.subject).get(MINE).json()["items"]
    assert [(i["id"], i["source"]) for i in mine] == [(body["id"], "org_admin")]
    assert _audit(superuser_engine, world.org_id) == [
        ("user.export.request", person.user_id, f"user:{world.subject.user_id}")
    ]


def test_members_may_not_ask_for_someone_elses_copy_or_list_the_orgs(
    world: ExportWorld,
) -> None:
    client = sign_in(world.harness, world.other)  # a team lead, an org member
    assert client.post(_member_path(world.subject.user_id)).status_code == 403
    assert client.get("/api/v1/org/exports").status_code == 403


def test_a_person_outside_the_org_is_no_member(world: ExportWorld) -> None:
    stranger = new_member(world.engine)
    client = sign_in(world.harness, world.admin)
    answer = client.post(_member_path(stranger.user_id))
    assert (answer.status_code, answer.json()["code"]) == (404, "no_such_member")
    assert client.post(_member_path(uuid.uuid4())).status_code == 404


def _unzip(body: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert archive.testzip() is None
        return {name: archive.read(name) for name in archive.namelist()}


def test_the_person_downloads_a_zip_whose_manifest_matches_every_entry(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    client = sign_in(world.harness, world.subject)
    export_id = client.post(MINE).json()["id"]
    early = client.get(_download(export_id))
    assert (early.status_code, early.json()["code"]) == (409, "export_not_ready")
    assert _build(world, superuser_engine, export_id) == "ready"
    listed = client.get(MINE).json()["items"][0]
    assert listed["canDownload"] is True and listed["bytes"] > 0 and listed["expiresAt"]

    answer = client.get(_download(export_id))

    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"] == "application/zip"
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["content-disposition"].startswith(
        'attachment; filename="munshi-export-'
    )
    entries = _unzip(answer.content)
    assert list(entries)[0] == "manifest.json"
    manifest = json.loads(entries["manifest.json"])
    assert {f["path"] for f in manifest["files"]} == set(entries) - {"manifest.json"}
    for entry in manifest["files"]:
        data = entries[entry["path"]]
        assert (len(data), hashlib.sha256(data).hexdigest()) == (entry["bytes"], entry["sha256"])
    recordings = sorted(p for p in entries if p.startswith("recordings/"))
    assert len(recordings) == 2
    assert sorted(entries[p] for p in recordings) == sorted(world.audio.values())
    meetings = [p for p in entries if p.startswith("meetings/")]
    assert len(meetings) == 3
    assert [a[0] for a in _audit(superuser_engine, world.org_id)] == [
        "user.export.request", "user.export.ready", "user.export.download"
    ]
    assert _audit(superuser_engine, world.org_id)[-1][1] == world.subject.user_id


def test_only_the_person_and_the_admin_who_asked_may_download(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    admin = sign_in(world.harness, world.admin)
    export_id = admin.post(_member_path(world.subject.user_id)).json()["id"]
    assert _build(world, superuser_engine, export_id) == "ready"

    assert admin.get(_download(export_id)).status_code == 200
    assert sign_in(world.harness, world.subject).get(_download(export_id)).status_code == 200
    owner = sign_in(world.harness, world.owner)
    other = sign_in(world.harness, world.other)
    outsider = sign_in(world.harness, new_member(world.engine))
    for client in (owner, other, outsider):
        refused = client.get(_download(export_id))
        assert (refused.status_code, refused.json()["code"]) == (404, "no_such_export")
    listed = {i["id"]: i["canDownload"] for i in owner.get("/api/v1/org/exports").json()["items"]}
    assert listed == {export_id: False}
    assert admin.get("/api/v1/org/exports").json()["items"][0]["canDownload"] is True
    assert sign_in(world.harness, world.other).get(MINE).json()["items"] == []
    assert owner.get(_download(str(uuid.uuid4()))).status_code == 404


def test_an_admin_who_is_no_longer_one_cannot_download(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    admin = sign_in(world.harness, world.admin)
    export_id = admin.post(_member_path(world.subject.user_id)).json()["id"]
    assert _build(world, superuser_engine, export_id) == "ready"
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE org_memberships SET role = 'member' WHERE user_id = :u"),
            {"u": world.admin.user_id},
        )
    assert admin.get(_download(export_id)).status_code == 404


def test_an_expired_copy_is_gone(world: ExportWorld, superuser_engine: Engine) -> None:
    client = sign_in(world.harness, world.subject)
    export_id = client.post(MINE).json()["id"]
    assert _build(world, superuser_engine, export_id) == "ready"

    world.harness.clock.advance(days=7, seconds=1)
    client = sign_in(world.harness, world.subject)  # the old session's absolute lifetime ran out
    late = client.get(_download(export_id))
    assert (late.status_code, late.json()["code"]) == (410, "export_expired")

    with superuser_engine.begin() as conn:
        conn.execute(text("SELECT andai_expire_exports(:at)"), {"at": world.harness.clock()})
    gone = client.get(_download(export_id))
    assert (gone.status_code, gone.json()["code"]) == (410, "export_expired")
    assert client.get(MINE).json()["items"][0]["state"] == "expired"


@pytest.mark.parametrize("change", ["bytes", "gone"])
def test_a_recording_that_changed_stops_the_download(
    world: ExportWorld, superuser_engine: Engine, change: str
) -> None:
    client = sign_in(world.harness, world.subject)
    export_id = client.post(MINE).json()["id"]
    assert _build(world, superuser_engine, export_id) == "ready"
    key = next(k for k in world.harness.gateway.media if k[0] in world.audio)
    if change == "bytes":
        original = world.harness.gateway.media[key]
        world.harness.gateway.media[key] = bytes(reversed(original))  # same size, other bytes
    else:
        del world.harness.gateway.media[key]

    answer = client.get(_download(export_id))

    assert (answer.status_code, answer.json()["code"]) == (502, "recording_changed")
    assert [a[0] for a in _audit(superuser_engine, world.org_id)][-1] == (
        "user.export.download_failed"
    )


def test_an_unreachable_platform_is_a_retryable_failure(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    client = sign_in(world.harness, world.subject)
    export_id = client.post(MINE).json()["id"]
    assert _build(world, superuser_engine, export_id) == "ready"
    world.harness.gateway.fail["stream recording"] = 503

    answer = client.get(_download(export_id))
    assert (answer.status_code, answer.json()["code"]) == (502, "platform_unavailable")

    del world.harness.gateway.fail["stream recording"]
    assert client.get(_download(export_id)).status_code == 200


# ─── one download at a time per person ────────────────────────────────────────────────────────────


class _HeldStream:
    """Wraps the gateway's `stream_recording`: the first call waits until `release()`; later calls
    pass straight through. `started` is set once the first call is waiting."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, gateway, *, raise_inside=None) -> None:
        self.started = threading.Event()
        self._go = threading.Event()
        self._first = True
        self._raise = raise_inside
        real = gateway.stream_recording

        async def held(*args, **kwargs):
            if self._first:
                self._first = False
                self.started.set()
                await anyio.to_thread.run_sync(self._go.wait)
                if self._raise is not None:
                    raise self._raise
            async for chunk in real(*args, **kwargs):
                yield chunk

        monkeypatch.setattr(gateway, "stream_recording", held)

    def release(self) -> None:
        self._go.set()


def _in_thread(client, path: str) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def run() -> None:
        try:
            out["response"] = client.get(path)
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def test_a_second_download_by_the_same_person_waits_its_turn(
    world: ExportWorld, superuser_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = sign_in(world.harness, world.subject)
    export_id = client.post(MINE).json()["id"]
    assert _build(world, superuser_engine, export_id) == "ready"
    held = _HeldStream(monkeypatch, world.harness.services.gateway)

    thread, first = _in_thread(sign_in(world.harness, world.subject), _download(export_id))
    assert held.started.wait(10)
    second = client.get(_download(export_id))
    held.release()
    thread.join(30)

    assert (second.status_code, second.json()["code"]) == (429, "export_download_busy")
    assert int(second.headers["retry-after"]) > 0
    assert first["response"].status_code == 200, first
    # the slot is free again once the first one finished
    assert client.get(_download(export_id)).status_code == 200


def test_another_person_is_not_blocked_by_someone_elses_download(
    world: ExportWorld, superuser_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = sign_in(world.harness, world.subject)
    mine = subject.post(MINE).json()["id"]
    assert _build(world, superuser_engine, mine) == "ready"
    admin = sign_in(world.harness, world.admin)
    asked = admin.post(_member_path(world.subject.user_id)).json()["id"]
    assert _build(world, superuser_engine, asked) == "ready"
    held = _HeldStream(monkeypatch, world.harness.services.gateway)

    thread, first = _in_thread(subject, _download(mine))
    assert held.started.wait(10)
    by_admin = admin.get(_download(asked))  # the same person's data, another downloader
    held.release()
    thread.join(30)

    assert by_admin.status_code == 200, by_admin.text
    assert first["response"].status_code == 200


def test_the_slot_is_released_after_a_failed_download(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    client = sign_in(world.harness, world.subject)
    export_id = client.post(MINE).json()["id"]
    assert _build(world, superuser_engine, export_id) == "ready"
    world.harness.gateway.fail["stream recording"] = 503

    assert client.get(_download(export_id)).status_code == 502

    del world.harness.gateway.fail["stream recording"]
    assert client.get(_download(export_id)).status_code == 200


def test_the_slot_is_released_when_the_download_is_cancelled(
    world: ExportWorld, superuser_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that goes away cancels the request while the zip is being built."""
    client = sign_in(world.harness, world.subject)
    export_id = client.post(MINE).json()["id"]
    assert _build(world, superuser_engine, export_id) == "ready"
    held = _HeldStream(
        monkeypatch, world.harness.services.gateway, raise_inside=asyncio.CancelledError()
    )

    thread, first = _in_thread(sign_in(world.harness, world.subject), _download(export_id))
    assert held.started.wait(10)
    held.release()
    thread.join(30)

    assert "response" not in first or first["response"].status_code >= 500
    assert client.get(_download(export_id)).status_code == 200


def test_the_number_of_downloads_at_once_is_a_setting(
    world: ExportWorld, superuser_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = world.harness.services
    monkeypatch.setattr(
        services, "settings", make_settings(ANDAI_EXPORT_DOWNLOADS_CONCURRENT="2")
    )
    client = sign_in(world.harness, world.subject)
    export_id = client.post(MINE).json()["id"]
    assert _build(world, superuser_engine, export_id) == "ready"
    held = _HeldStream(monkeypatch, services.gateway)

    thread, first = _in_thread(sign_in(world.harness, world.subject), _download(export_id))
    assert held.started.wait(10)
    second = client.get(_download(export_id))
    held.release()
    thread.join(30)

    assert second.status_code == 200, second.text
    assert first["response"].status_code == 200


@pytest.fixture(params=["memory", "valkey"])
def slots(request: pytest.FixtureRequest):
    clock = {"now": 1_000_000.0}

    def now() -> float:
        return clock["now"]

    if request.param == "memory":
        return MemorySlots(clock=now), clock
    client = redis.Redis.from_url(request.getfixturevalue("valkey_url"))
    return ValkeySlots(client, clock=now, prefix=f"test:{uuid.uuid4().hex}:"), clock


def test_slots_admit_up_to_the_limit_release_and_expire(slots) -> None:
    store, clock = slots
    a = store.take("download:a", limit=1, ttl=900)
    assert a is not None
    assert store.take("download:a", limit=1, ttl=900) is None
    assert store.take("download:b", limit=1, ttl=900) is not None  # keys are independent
    second = store.take("download:a", limit=2, ttl=900)
    assert second is not None
    store.release("download:a", a)
    store.release("download:a", second)
    again = store.take("download:a", limit=1, ttl=900)
    assert again is not None
    clock["now"] += 901  # a slot never released (a crashed process) expires
    assert store.take("download:a", limit=1, ttl=900) is not None


def test_the_listings_carry_no_address_or_content(
    world: ExportWorld, superuser_engine: Engine
) -> None:
    admin = sign_in(world.harness, world.admin)
    export_id = admin.post(_member_path(world.subject.user_id)).json()["id"]
    assert _build(world, superuser_engine, export_id) == "ready"
    text_of = json.dumps(admin.get("/api/v1/org/exports").json()) + json.dumps(
        sign_in(world.harness, world.subject).get(MINE).json()
    )
    assert world.subject.email not in text_of
    assert "Quarterly budget review" not in text_of
    assert set(admin.get("/api/v1/org/exports").json()["items"][0]) == {
        "id", "subjectId", "subjectName", "source", "state", "error", "createdAt", "readyAt",
        "expiresAt", "bytes", "canDownload",
    }


def test_the_org_listing_pages_by_fifty(world: ExportWorld, superuser_engine: Engine) -> None:
    with superuser_engine.begin() as conn:
        for n in range(51):
            conn.execute(
                text(
                    "INSERT INTO export_requests (org_id, subject_user_id, requested_by, source,"
                    " state, error, created_at) VALUES (:o, :s, :a, 'org_admin', 'failed',"
                    " 'internal_error', now() - make_interval(mins => :n))"
                ),
                {"o": world.org_id, "s": world.subject.user_id, "a": world.admin.user_id, "n": n},
            )
    admin = sign_in(world.harness, world.admin)
    first = admin.get("/api/v1/org/exports").json()
    assert len(first["items"]) == 50 and first["next"]
    second = admin.get("/api/v1/org/exports", params={"cursor": first["next"]}).json()
    assert len(second["items"]) == 1 and second["next"] is None
    bad = admin.get("/api/v1/org/exports", params={"cursor": "nonsense"})
    assert (bad.status_code, bad.json()["code"]) == (422, "bad_cursor")
