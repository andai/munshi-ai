"""#116 — completed meeting transcript + recording-object erasure."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.collector.fakes import InMemoryTranscriptStore
from meeting_api.recordings.fakes import InMemoryStorage


OWNER = 7
OTHER = 8
MEETING_ID = 41
RECORDING_ID = 9001
PREFIX = f"recordings/{OWNER}/{RECORDING_ID}/sess-41/audio/"


def _recording() -> dict:
    return {
        "id": RECORDING_ID,
        "meeting_id": MEETING_ID,
        "user_id": OWNER,
        "session_uid": "sess-41",
        "status": "completed",
        "media_files": [{
            "id": 22,
            "type": "audio",
            "format": "wav",
            "storage_path": f"{PREFIX}master.wav",
        }],
    }


def _fixture(*, status: str = "completed", storage_cls=InMemoryStorage):
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        meeting_id=MEETING_ID,
        user_id=OWNER,
        platform="google_meet",
        native_meeting_id="private-room",
        status=status,
        data={
            "recordings": [_recording()],
            "processed": {"views": [{"doc": {"notes": ["derived"]}}]},
            "notes": "derived summary",
            "share_grants": [{"id": "share"}],
            "transcript_viewers": [OTHER],
        },
        segments=[{
            "segment_id": "s1", "start": 0, "end": 1,
            "text": "confidential", "language": "en",
        }],
    )
    storage = storage_cls()
    storage.blobs[f"{PREFIX}000000.wav"] = b"chunk"
    storage.blobs[f"{PREFIX}master.wav"] = b"master"
    return store, storage, TestClient(
        create_app(transcript_store=store, storage=storage),
        raise_server_exceptions=False,
    )


def test_owner_deletes_completed_artifacts_but_terminal_meeting_row_survives():
    store, storage, client = _fixture()

    response = client.delete(
        f"/meetings/{MEETING_ID}", headers={"x-user-id": str(OWNER)}
    )
    assert response.status_code == 204
    assert storage.blobs == {}
    assert client.get(
        "/transcripts/google_meet/private-room", headers={"x-user-id": str(OWNER)}
    ).status_code == 404

    meeting = store._meetings[MEETING_ID]
    assert meeting["status"] == "completed", "terminal lifecycle evidence is retained"
    assert meeting["segments"] == {}
    assert "recordings" not in meeting["data"]
    assert "processed" not in meeting["data"]
    assert "notes" not in meeting["data"]
    assert meeting["data"]["artifact_deletion"]["backup_residuals"] == (
        "expire_under_deployment_retention_policy"
    )


def test_non_owner_gets_indistinguishable_404_and_cannot_delete_any_artifact():
    store, storage, client = _fixture()

    response = client.delete(
        f"/meetings/{MEETING_ID}", headers={"x-user-id": str(OTHER)}
    )
    assert response.status_code == 404
    assert sorted(storage.blobs) == [f"{PREFIX}000000.wav", f"{PREFIX}master.wav"]
    assert store._meetings[MEETING_ID]["segments"]["s1"]["text"] == "confidential"


def test_storage_failure_preserves_paths_and_transcript_for_retry():
    class FailsOnceStorage(InMemoryStorage):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def delete(self, key: str) -> None:
            if self.fail:
                self.fail = False
                raise RuntimeError("injected object-store failure")
            await super().delete(key)

    store, storage, client = _fixture(storage_cls=FailsOnceStorage)

    first = client.delete(f"/meetings/{MEETING_ID}", headers={"x-user-id": str(OWNER)})
    assert first.status_code == 500
    assert store._meetings[MEETING_ID]["data"]["recordings"][0]["id"] == RECORDING_ID
    assert store._meetings[MEETING_ID]["data"]["artifact_deletion"]["state"] == "pending"
    assert client.get(
        "/transcripts/google_meet/private-room", headers={"x-user-id": str(OWNER)}
    ).status_code == 200

    retry = client.delete(f"/meetings/{MEETING_ID}", headers={"x-user-id": str(OWNER)})
    assert retry.status_code == 204
    assert storage.blobs == {}
    assert "recordings" not in store._meetings[MEETING_ID]["data"]
    assert store._meetings[MEETING_ID]["data"]["artifact_deletion"]["state"] == "completed"


def test_completed_artifact_delete_is_idempotent_and_active_lifecycle_is_not_deleted():
    store, storage, client = _fixture()
    headers = {"x-user-id": str(OWNER)}
    assert client.delete(f"/meetings/{MEETING_ID}", headers=headers).status_code == 204
    assert client.delete(f"/meetings/{MEETING_ID}", headers=headers).status_code == 204

    active_store, active_storage, active_client = _fixture(status="active")
    response = active_client.delete(f"/meetings/{MEETING_ID}", headers=headers)
    assert response.status_code == 409
    assert active_store._meetings[MEETING_ID]["status"] == "active"
    assert active_storage.blobs


ORG_PREFIX = "org/3f2b8c1e-9a4d-4e7f-8b21-5c6d7e8f9a0b"
OTHER_ORG_PREFIX = "org/7c9d0e1f-2a3b-4c5d-8e6f-a1b2c3d4e5f6"


def test_meeting_delete_removes_the_prefixed_objects_and_only_those():
    store = InMemoryTranscriptStore()
    prefixed = f"{ORG_PREFIX}/{PREFIX}"
    recording = {**_recording(), "media_files": [{
        "id": 22, "type": "audio", "format": "wav", "storage_path": f"{prefixed}master.wav",
    }]}
    store.seed_meeting(
        meeting_id=MEETING_ID, user_id=OWNER, platform="google_meet",
        native_meeting_id="private-room", status="completed",
        data={"recordings": [recording], "storage_prefix": ORG_PREFIX},
    )
    storage = InMemoryStorage()
    storage.blobs[f"{prefixed}000000.wav"] = b"chunk"
    storage.blobs[f"{prefixed}master.wav"] = b"master"
    # Same owner id and recording id under another org's prefix, and the legacy layout.
    survivors = {f"{OTHER_ORG_PREFIX}/{PREFIX}000000.wav", f"{PREFIX}000000.wav"}
    for key in survivors:
        storage.blobs[key] = b"other"
    client = TestClient(create_app(transcript_store=store, storage=storage),
                        raise_server_exceptions=False)

    assert client.delete(f"/meetings/{MEETING_ID}",
                         headers={"x-user-id": str(OTHER)}).status_code == 404
    response = client.delete(f"/meetings/{MEETING_ID}", headers={"x-user-id": str(OWNER)})
    assert response.status_code == 204
    assert set(storage.blobs) == survivors
    assert "recordings" not in store._meetings[MEETING_ID]["data"]


# ── the live Redis state of a deleted meeting ────────────────────────────────────────────────────


async def _redis_fixture():
    import fakeredis.aioredis

    redis = fakeredis.aioredis.FakeRedis()
    store = InMemoryTranscriptStore(redis_client=redis)
    store.seed_meeting(
        meeting_id=MEETING_ID, user_id=OWNER, platform="google_meet",
        native_meeting_id="private-room", status="completed",
    )
    # What a finished meeting leaves in Valkey: the per-meeting transcript stream the copilot worker
    # and the terminal read, the live segment hash and the processed-notes stream.
    await redis.xadd(f"tc:meeting:{MEETING_ID}", {"payload": json.dumps({"text": "confidential"})})
    await redis.xadd(f"tc:meeting:{MEETING_ID}", {"type": "session_end", "uid": "private-room"})
    await redis.hset(f"meeting:{MEETING_ID}:segments", "s1", json.dumps({"text": "confidential"}))
    await redis.xadd(f"proc:meeting:{MEETING_ID}", {"payload": "{}"})
    await redis.xadd("tc:meeting:42", {"payload": json.dumps({"text": "another meeting"})})
    return redis, store


async def test_finalize_deletes_the_meetings_transcript_stream():
    redis, store = await _redis_fixture()

    assert await store.finalize_completed_artifact_deletion(OWNER, MEETING_ID) is True

    assert await redis.exists(f"tc:meeting:{MEETING_ID}") == 0
    assert await redis.exists(f"meeting:{MEETING_ID}:segments", f"proc:meeting:{MEETING_ID}") == 0
    assert await redis.exists("tc:meeting:42") == 1                  # other meetings untouched
    await redis.aclose()


async def test_the_live_state_purge_names_every_per_meeting_key():
    """Both stores (SQL adapter and fake) purge through this one helper."""
    from meeting_api.collector.db_writer import purge_meeting_live_state

    redis, _ = await _redis_fixture()
    await redis.sadd("active_meetings", str(MEETING_ID), "42")

    await purge_meeting_live_state(redis, MEETING_ID)

    for key in (f"tc:meeting:{MEETING_ID}", f"meeting:{MEETING_ID}:segments",
                f"proc:meeting:{MEETING_ID}"):
        assert await redis.exists(key) == 0, key
    assert await redis.smembers("active_meetings") == {b"42"}
    assert await redis.exists("tc:meeting:42") == 1
    await redis.aclose()


def test_the_sql_store_purges_through_the_shared_helper():
    """The SQL adapter cannot run here (no SQLAlchemy in this venv); its finalize must call the
    helper the fake's test exercises, not a key list of its own."""
    import inspect

    from meeting_api.collector.adapters import SqlAlchemyTranscriptStore

    source = inspect.getsource(SqlAlchemyTranscriptStore.finalize_completed_artifact_deletion)
    assert "purge_meeting_live_state(self._redis, meeting_id)" in source
