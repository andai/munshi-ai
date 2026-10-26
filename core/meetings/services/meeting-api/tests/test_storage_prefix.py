"""Per-meeting object-storage prefix (``meeting.data.storage_prefix = "org/<uuid>"``).

An org principal's identity record carries ``storage_prefix``; the spawn snapshots it from
bot-context onto the meeting, and every object written for that meeting — recording chunks, the
master, captured-signal tapes — is keyed under it. Meetings without a prefix keep the unprefixed
``recordings/…`` and ``signal/…`` layout. OFFLINE over the in-memory fakes.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meeting_api.bot_spawn import request_bot
from meeting_api.bot_spawn import service as spawn_service
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.collector.projection import project_response_data
from meeting_api.recordings import (
    InvalidStoragePrefix,
    build_router,
    chunk_storage_key,
    finalize_master,
    master_storage_key,
    normalize_storage_prefix,
    signal_tape_key,
    signal_tape_prefix,
    signal_tape_roots,
    sweep_signal_tapes,
    upload_chunk,
    upload_signal_tape,
)
from meeting_api.recordings.deletion import recording_object_keys
from meeting_api.recordings.fakes import InMemoryRecordingRepo, InMemoryStorage
from meeting_api.recordings.signal_janitor import _tape_prefix

SECRET = "test-admin-token"
ORG_A = "org/3f2b8c1e-9a4d-4e7f-8b21-5c6d7e8f9a0b"
ORG_B = "org/7c9d0e1f-2a3b-4c5d-8e6f-a1b2c3d4e5f6"
USER_A, USER_B = 11, 12
MEETING_A, MEETING_B = 101, 102
SESSION_A, SESSION_B = "conn-a", "conn-b"


def _wav(n_data: int = 4) -> bytes:
    import struct

    data = b"\x00" * n_data
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16)
    chunk = struct.pack("<4sI", b"data", len(data)) + data
    return struct.pack("<4sI4s", b"RIFF", 4 + len(fmt) + len(chunk), b"WAVE") + fmt + chunk


# ── the shape check ──────────────────────────────────────────────────────────────────────────────

def test_normalize_accepts_absent_and_org_uuid():
    assert normalize_storage_prefix(None) is None
    assert normalize_storage_prefix("") is None
    assert normalize_storage_prefix(ORG_A) == ORG_A


@pytest.mark.parametrize("bad", [
    "org/x", "../org/3f2b8c1e-9a4d-4e7f-8b21-5c6d7e8f9a0b", "/" + ORG_A, ORG_A + "/",
    ORG_A + "/recordings", ORG_A.upper(), "org/" + "-" * 36, "recordings", 7, ["org"],
])
def test_normalize_rejects_everything_else(bad):
    with pytest.raises(InvalidStoragePrefix):
        normalize_storage_prefix(bad)


# ── key builders: prefix present / absent / malformed ────────────────────────────────────────────

def test_chunk_and_master_keys_without_prefix_are_unchanged():
    key = chunk_storage_key(user_id=7, recording_id=9, session_uid="s", media_type="audio",
                            media_format="wav", chunk_seq=3)
    assert key == "recordings/7/9/s/audio/000003.wav"
    assert master_storage_key(key, "wav") == "recordings/7/9/s/audio/master.wav"


def test_chunk_and_master_keys_with_prefix():
    key = chunk_storage_key(user_id=7, recording_id=9, session_uid="s", media_type="audio",
                            media_format="wav", chunk_seq=3, storage_prefix=ORG_A)
    assert key == f"{ORG_A}/recordings/7/9/s/audio/000003.wav"
    assert master_storage_key(key, "wav") == f"{ORG_A}/recordings/7/9/s/audio/master.wav"


def test_tape_keys_with_and_without_prefix():
    assert signal_tape_prefix(user_id=7, meeting_id=1, session_uid="s") == "signal/7/1/s/"
    assert signal_tape_prefix(user_id=7, meeting_id=1, session_uid="s",
                              storage_prefix=ORG_A) == f"{ORG_A}/signal/7/1/s/"
    assert signal_tape_key(user_id=7, meeting_id=1, session_uid="s", part="stt",
                           storage_prefix=ORG_A) == f"{ORG_A}/signal/7/1/s/stt.jsonl"


@pytest.mark.parametrize("bad", ["org/x", "../etc", "/abs", ORG_A + "/.."])
def test_builders_refuse_a_malformed_prefix(bad):
    with pytest.raises(InvalidStoragePrefix):
        chunk_storage_key(user_id=7, recording_id=9, session_uid="s", media_type="audio",
                          media_format="wav", chunk_seq=0, storage_prefix=bad)
    with pytest.raises(InvalidStoragePrefix):
        signal_tape_key(user_id=7, meeting_id=1, session_uid="s", part="stt", storage_prefix=bad)


# ── the write paths read the prefix from the meeting row ────────────────────────────────────────

def _two_orgs(status: str = "active"):
    repo = InMemoryRecordingRepo()
    repo.seed(meeting_id=MEETING_A, user_id=USER_A, session_uid=SESSION_A, status=status,
              storage_prefix=ORG_A)
    repo.seed(meeting_id=MEETING_B, user_id=USER_B, session_uid=SESSION_B, status=status,
              storage_prefix=ORG_B)
    return repo, InMemoryStorage()


async def test_chunks_master_and_tapes_land_under_the_meeting_prefix():
    repo, storage = _two_orgs()
    for mid, sess in ((MEETING_A, SESSION_A), (MEETING_B, SESSION_B)):
        for seq in (0, 1):
            await upload_chunk(repo, storage, token_meeting_id=mid, session_uid=sess,
                               data=_wav(), media_format="wav", chunk_seq=seq,
                               is_final=seq == 1)
        rec = (await repo.get_recordings(mid))[0]
        await finalize_master(repo, storage, meeting_id=mid, recording_id=rec["id"])
        await upload_signal_tape(repo, storage, token_meeting_id=mid, session_uid=sess,
                                 data=b"{}\n", part="captured-signal")

    keys = sorted(storage.blobs)
    a_keys = [k for k in keys if k.startswith(ORG_A + "/")]
    b_keys = [k for k in keys if k.startswith(ORG_B + "/")]
    assert sorted(a_keys + b_keys) == keys, "every object sits under one org prefix"
    for prefix, user, org_keys in ((ORG_A, USER_A, a_keys), (ORG_B, USER_B, b_keys)):
        assert sum(k.endswith("/master.wav") for k in org_keys) == 1
        assert sum(k.endswith(".wav") and "/master." not in k for k in org_keys) == 2
        assert any(k.startswith(f"{prefix}/signal/{user}/") for k in org_keys)
        assert all(k.startswith((f"{prefix}/recordings/{user}/", f"{prefix}/signal/{user}/"))
                   for k in org_keys)


async def test_an_unprefixed_meeting_keeps_the_legacy_layout():
    repo, storage = InMemoryRecordingRepo(), InMemoryStorage()
    repo.seed(meeting_id=1, user_id=7, session_uid="s")
    receipt = await upload_chunk(repo, storage, token_meeting_id=1, session_uid="s",
                                 data=_wav(), media_format="wav", chunk_seq=0, is_final=True)
    assert receipt["storage_path"].startswith("recordings/7/")
    tape = await upload_signal_tape(repo, storage, token_meeting_id=1, session_uid="s",
                                    data=b"{}\n", part="stt")
    assert tape["storage_path"].startswith("signal/7/1/s/")


async def test_the_upload_request_cannot_choose_a_prefix():
    """The bot's upload names a session and media fields only; extra metadata is ignored and the
    key comes from the meeting row."""
    from meeting_api.bot_spawn import mint_meeting_token

    repo, storage = _two_orgs()
    app = FastAPI()
    app.include_router(build_router(repo, storage, token_secret=SECRET))
    token = mint_meeting_token(MEETING_A, USER_A, "google_meet", "abc", secret=SECRET)
    r = TestClient(app).post(
        "/internal/recordings/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("chunk.wav", _wav(), "audio/wav")},
        data={"metadata": json.dumps({"session_uid": SESSION_A, "format": "wav",
                                      "storage_prefix": ORG_B}),
              "storage_prefix": ORG_B},
    )
    assert r.status_code == 200, r.text
    assert r.json()["storage_path"].startswith(f"{ORG_A}/recordings/{USER_A}/")
    assert not any(k.startswith(ORG_B) for k in storage.blobs)


# ── deletion lists the meeting's own keyspace only ───────────────────────────────────────────────

async def test_recording_delete_removes_every_prefixed_object_and_nothing_of_the_other_org():
    repo, storage = _two_orgs(status="active")
    for mid, sess in ((MEETING_A, SESSION_A), (MEETING_B, SESSION_B)):
        await upload_chunk(repo, storage, token_meeting_id=mid, session_uid=sess,
                           data=_wav(), media_format="wav", chunk_seq=0, is_final=True)
        rec = (await repo.get_recordings(mid))[0]
        await finalize_master(repo, storage, meeting_id=mid, recording_id=rec["id"])
        repo._meetings[mid]["status"] = "completed"
    rid_a = (await repo.get_recordings(MEETING_A))[0]["id"]
    rid_b = (await repo.get_recordings(MEETING_B))[0]["id"]
    b_before = sorted(k for k in storage.blobs if k.startswith(ORG_B))

    app = FastAPI()
    app.include_router(build_router(repo, storage, token_secret=SECRET))
    client = TestClient(app)
    # Org A's principal cannot read, download or delete org B's recording.
    a_headers = {"x-user-id": str(USER_A)}
    assert client.get(f"/recordings/{rid_b}", headers=a_headers).status_code == 404
    assert client.delete(f"/recordings/{rid_b}", headers=a_headers).status_code == 404
    assert all(str(rid_b) not in json.dumps(r) for r in
               client.get("/recordings", headers=a_headers).json()["recordings"])

    r = client.delete(f"/recordings/{rid_a}", headers=a_headers)
    assert r.status_code == 200, r.text
    assert r.json()["objects_deleted"] == 2  # chunk + master
    assert not any(k.startswith(ORG_A) for k in storage.blobs)
    assert sorted(k for k in storage.blobs if k.startswith(ORG_B)) == b_before
    # The prefix the repository attached for the deletion was not written back.
    assert all("storage_prefix" not in r for r in await repo.get_recordings(MEETING_B))


async def test_deletion_never_reaches_past_the_owner_namespace_inside_a_prefix():
    storage = InMemoryStorage()
    own = f"{ORG_A}/recordings/{USER_A}/9/s/audio/master.wav"
    foreign = f"{ORG_B}/recordings/{USER_B}/9/s/audio/master.wav"
    legacy = f"recordings/{USER_A}/9/s/audio/master.wav"
    for k in (own, foreign, legacy):
        storage.blobs[k] = b"x"
    rec = {"id": 9, "user_id": USER_A, "session_uid": "s", "storage_prefix": ORG_A,
           "media_files": [{"storage_path": p} for p in (own, foreign, legacy)]}
    assert await recording_object_keys(storage, rec) == [own]


async def test_deletion_refuses_a_malformed_prefix():
    rec = {"id": 9, "user_id": USER_A, "session_uid": "s", "storage_prefix": "../x",
           "media_files": []}
    with pytest.raises(InvalidStoragePrefix):
        await recording_object_keys(InMemoryStorage(), rec)


# ── the tape janitor sees prefixed tapes ─────────────────────────────────────────────────────────

def test_tape_prefix_recognizes_both_layouts_and_nothing_else():
    assert _tape_prefix("signal/7/1/s/stt.jsonl") == "signal/7/1/s/"
    assert _tape_prefix(f"{ORG_A}/signal/7/1/s/stt.jsonl") == f"{ORG_A}/signal/7/1/s/"
    assert _tape_prefix(f"{ORG_A}/recordings/7/1/s/a.wav") is None
    assert _tape_prefix("org/x/signal/7/1/s/stt.jsonl") is None
    assert _tape_prefix(f"{ORG_A}/signal/7/1/s/") is None


async def test_janitor_budget_covers_every_signal_root():
    storage = InMemoryStorage()
    legacy = "signal/7/1/s/captured-signal.jsonl"
    org_tape = f"{ORG_A}/signal/{USER_A}/{MEETING_A}/s/captured-signal.jsonl"
    org_recording = f"{ORG_A}/recordings/{USER_A}/9/s/audio/master.wav"
    stray = "org/not-a-uuid/signal/7/1/s/captured-signal.jsonl"
    for key, mtime in ((legacy, 100.0), (org_tape, 50.0), (org_recording, 1.0), (stray, 1.0)):
        storage.blobs[key] = b"x" * 100
        storage.mtimes[key] = mtime

    assert await signal_tape_roots(storage) == ["signal/", f"{ORG_A}/signal/"]
    out = await sweep_signal_tapes(storage, budget_bytes=150, now=1_000_000.0)
    assert out["tapes"] == 2
    assert out["evicted"] == 1
    # The oldest tape went, and it was the org's; recordings and stray keys are never touched.
    assert org_tape not in storage.blobs
    assert legacy in storage.blobs and org_recording in storage.blobs and stray in storage.blobs


# ── the spawn snapshots the prefix from identity ─────────────────────────────────────────────────

async def _spawn(monkeypatch, repo, ctx, *, native="abc-defg-hij", continue_meeting=False):
    async def fake_ctx(_user_id):
        return ctx

    monkeypatch.setenv("ADMIN_TOKEN", SECRET)
    monkeypatch.setattr(spawn_service, "_fetch_bot_context", fake_ctx)
    return await request_bot(repo, FakeRuntimeClient(), user_id=USER_A, platform="google_meet",
                             native_meeting_id=native, redis_url="redis://redis:6379/0",
                             token_secret=SECRET, continue_meeting=continue_meeting)


@pytest.mark.parametrize("ctx,expected", [
    ({"storage_prefix": ORG_A}, ORG_A),
    ({}, None),
    ({"storage_prefix": "../org/x"}, None),
    ({"storage_prefix": ORG_A + "/recordings"}, None),
])
async def test_spawn_snapshots_only_a_well_formed_prefix(monkeypatch, ctx, expected):
    repo = InMemoryMeetingRepo()
    row = await _spawn(monkeypatch, repo, ctx)
    data = (await repo.get_meeting(row["id"]))["data"]
    assert data.get("storage_prefix") == expected


async def test_a_reopened_meeting_keeps_its_first_layout(monkeypatch):
    repo = InMemoryMeetingRepo()
    first = await _spawn(monkeypatch, repo, {"storage_prefix": ORG_A})
    repo.set_status(first["id"], "completed")
    again = await _spawn(monkeypatch, repo, {}, continue_meeting=True)
    assert again["id"] == first["id"]
    assert (await repo.get_meeting(first["id"]))["data"]["storage_prefix"] == ORG_A


def test_prefix_is_served_to_the_owner_only():
    data = {"storage_prefix": ORG_A, "recording_enabled": True}
    assert project_response_data(data, viewer_is_owner=True)["storage_prefix"] == ORG_A
    assert "storage_prefix" not in project_response_data(data, viewer_is_owner=False)


async def test_s3_list_children_uses_a_delimiter_and_pages_to_exhaustion():
    """The janitor finds org prefixes with one delimiter listing per page — it never lists the
    objects inside them."""
    from meeting_api.recordings.adapters import S3Storage

    orgs = [f"org/{i:08x}-0000-4000-8000-000000000000/" for i in range(3)]
    calls: list[dict] = []

    class _Client:
        def list_objects_v2(self, **kw):
            calls.append(kw)
            if kw.get("ContinuationToken") is None:
                return {"CommonPrefixes": [{"Prefix": p} for p in orgs[:2]],
                        "IsTruncated": True, "NextContinuationToken": "t1"}
            return {"CommonPrefixes": [{"Prefix": orgs[2]}], "IsTruncated": False}

    class _Stub(S3Storage):
        def __init__(self):
            super().__init__(bucket="b")

        def _c(self):
            return _Client()

    assert await _Stub().list_children("org/") == orgs
    assert len(calls) == 2
    assert all(c["Delimiter"] == "/" and c["Prefix"] == "org/" for c in calls)
