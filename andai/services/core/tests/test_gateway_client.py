"""The gateway client: the org key on every call, typed errors that never carry a key or a body."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from andai_core.platform.gateway_client import (
    API_KEY_HEADER,
    DuplicateMeeting,
    GatewayError,
    HttpPlatformGateway,
    RecordingTooLarge,
    live_url,
)

KEY = "tok_bot_" + "k" * 32
SECRET_BODY = "pwd=s3cretPwd"


def gateway(handler) -> HttpPlatformGateway:
    return HttpPlatformGateway("http://gateway.test/", transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def test_dispatch_posts_the_body_with_the_org_key() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 7, "status": "requested"})

    body = {"meeting_url": f"https://zoom.us/j/12345678901?{SECRET_BODY}", "language": "en"}
    answer = run(gateway(handler).dispatch(KEY, body))

    assert answer == {"id": 7, "status": "requested"}
    [request] = seen
    assert (request.method, request.url.path) == ("POST", "/bots")
    assert request.headers[API_KEY_HEADER] == KEY
    assert json.loads(request.content) == body


def test_structured_duplicate_carries_the_platform_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "detail": "An active or requested meeting already exists",
                "code": "duplicate_meeting",
                "meeting_id": 41,
                "platform": "zoom",
                "native_meeting_id": "12345678901",
            },
        )

    with pytest.raises(DuplicateMeeting) as caught:
        run(gateway(handler).dispatch(KEY, {"meeting_url": "x"}))

    assert (caught.value.meeting_id, caught.value.platform, caught.value.native_meeting_id) == (
        41, "zoom", "12345678901"
    )
    assert caught.value.status == 409


def test_a_409_without_the_code_is_a_plain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "auth session busy"})

    with pytest.raises(GatewayError) as caught:
        run(gateway(handler).dispatch(KEY, {"meeting_url": "x"}))

    assert not isinstance(caught.value, DuplicateMeeting)
    assert caught.value.status == 409


@pytest.mark.parametrize("status", [401, 422, 429, 500, 502, 503])
def test_errors_name_the_call_and_status_only(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": f"echo {KEY} {SECRET_BODY}"})

    with pytest.raises(GatewayError) as caught:
        run(gateway(handler).dispatch(KEY, {"meeting_url": SECRET_BODY}))

    assert caught.value.status == status
    assert str(caught.value) == f"gateway dispatch answered {status}"
    assert KEY not in repr(caught.value) and SECRET_BODY not in repr(caught.value)


def test_unreachable_gateway_has_no_status_and_no_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect {KEY}", request=request)

    with pytest.raises(GatewayError) as caught:
        run(gateway(handler).list_bots(KEY))

    assert caught.value.status is None
    assert str(caught.value) == "gateway list bots: ConnectError"
    assert caught.value.__cause__ is None and caught.value.__context__ is not None
    assert KEY not in str(caught.value)


def test_list_get_and_stop() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode(), request.headers[API_KEY_HEADER]))
        if request.url.path == "/bots":
            return httpx.Response(200, json={"meetings": [{"id": 1}, "junk"], "has_more": False})
        if request.url.path == "/meetings/9":
            return httpx.Response(200, json={"id": 9, "status": "active"})
        return httpx.Response(202, json={"message": "Stop requested"})

    client = gateway(handler)
    assert run(client.list_bots(KEY)) == [{"id": 1}]
    assert run(client.get_meeting(KEY, 9)) == {"id": 9, "status": "active"}
    run(client.stop(KEY, "teams", "19:meeting_abc@thread.v2"))

    assert seen == [
        ("GET", "/bots?limit=100", KEY),
        ("GET", "/meetings/9", KEY),
        ("DELETE", "/bots/teams/19%3Ameeting_abc%40thread.v2", KEY),
    ]


def test_a_missing_key_is_refused_before_any_call() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(GatewayError, match="no API key"):
        run(gateway(handler).get_meeting("", 1))
    assert calls == []


def test_a_non_object_answer_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(GatewayError, match="unexpected body"):
        run(gateway(handler).get_meeting(KEY, 1))


def test_get_transcript_reads_by_platform_meeting_id_with_the_org_key() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/transcripts/by-id/404":
            return httpx.Response(404, json={"detail": "Meeting 404 not found"})
        return httpx.Response(200, json={"id": 42, "segments": []})

    assert run(gateway(handler).get_transcript(KEY, 42)) == {"id": 42, "segments": []}
    with pytest.raises(GatewayError) as caught:
        run(gateway(handler).get_transcript(KEY, 404))

    assert (seen[0].method, seen[0].url.path) == ("GET", "/transcripts/by-id/42")
    assert seen[0].headers[API_KEY_HEADER] == KEY
    assert caught.value.status == 404 and KEY not in str(caught.value)


def test_delete_meeting_deletes_by_platform_meeting_id_with_the_org_key() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    assert run(gateway(handler).delete_meeting(KEY, 41)) is None

    [request] = seen
    assert (request.method, request.url.path) == ("DELETE", "/meetings/41")
    assert request.headers[API_KEY_HEADER] == KEY


@pytest.mark.parametrize("status", [404, 409, 500, 503])
def test_delete_meeting_refusals_carry_the_status(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": f"no {KEY}"})

    with pytest.raises(GatewayError) as caught:
        run(gateway(handler).delete_meeting(KEY, 41))

    assert (caught.value.what, caught.value.status) == ("delete meeting", status)
    assert KEY not in str(caught.value)


def test_get_participants_reads_by_platform_and_native_id_with_the_org_key() -> None:
    seen = []
    answer = {
        "meeting_id": 42,
        "platform": "teams",
        "native_meeting_id": "19:meeting_abc@thread.v2",
        "participants": [{"name": "Asha", "email": None, "source": "speaker"}],
        "sources": ["speaker"],
        "observed_roster": "not_recorded",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "missing" in request.url.path:
            return httpx.Response(404, json={"detail": "Meeting not found"})
        return httpx.Response(200, json=answer)

    client = gateway(handler)
    assert run(client.get_participants(KEY, "teams", "19:meeting_abc@thread.v2")) == answer
    with pytest.raises(GatewayError) as caught:
        run(client.get_participants(KEY, "zoom", "missing"))

    assert seen[0].method == "GET"
    assert seen[0].url.raw_path == b"/meetings/teams/19%3Ameeting_abc%40thread.v2/participants"
    assert seen[0].headers[API_KEY_HEADER] == KEY
    assert caught.value.what == "get participants"
    assert caught.value.status == 404 and KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("base", "url"),
    [
        ("http://gateway:8000", "ws://gateway:8000/ws"),
        ("http://gateway:8000/", "ws://gateway:8000/ws"),
        ("https://gw.example.test/platform", "wss://gw.example.test/platform/ws"),
    ],
)
def test_live_url_is_the_gateway_websocket(base: str, url: str) -> None:
    assert live_url(base) == url


def test_list_recordings_reads_one_meetings_recordings_with_the_org_key() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"recordings": [{"id": 3, "media_files": []}, "junk"], "total": 1}
        )

    assert run(gateway(handler).list_recordings(KEY, 41)) == [{"id": 3, "media_files": []}]
    [request] = seen
    assert (request.method, request.url.path) == ("GET", "/recordings")
    assert request.url.params["meeting_id"] == "41"
    assert request.headers[API_KEY_HEADER] == KEY


@pytest.mark.parametrize("status", [401, 500])
def test_list_recordings_refusals_carry_the_status(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": KEY})

    with pytest.raises(GatewayError) as caught:
        run(gateway(handler).list_recordings(KEY, 41))
    assert caught.value.status == status
    assert KEY not in str(caught.value)


def _recording_server(body: bytes, seen: list, *, master_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/recordings/3/master":
            if master_status != 200:
                return httpx.Response(master_status, json={"detail": "No such media file"})
            return httpx.Response(200, json={"id": 3, "media_file_id": 9, "type": "audio"})
        if request.url.path == "/recordings/3/media/9/raw":
            return httpx.Response(200, content=body, headers={"content-type": "audio/wav"})
        return httpx.Response(404)

    return handler


async def _collect(stream) -> list[bytes]:
    return [chunk async for chunk in stream]


def test_stream_recording_finalises_the_master_then_reads_it_in_chunks() -> None:
    from andai_core.platform import gateway_client

    body = bytes(range(256)) * (gateway_client.CHUNK_BYTES // 256 * 2 + 3)
    seen: list = []
    chunks = run(
        _collect(
            gateway(_recording_server(body, seen)).stream_recording(
                KEY, 3, media_type="audio", max_bytes=len(body)
            )
        )
    )
    assert b"".join(chunks) == body
    assert len(chunks) >= 3
    assert max(len(c) for c in chunks) <= gateway_client.CHUNK_BYTES
    assert [r.url.path for r in seen] == ["/recordings/3/master", "/recordings/3/media/9/raw"]
    assert seen[0].url.params["type"] == "audio"
    assert all(r.headers[API_KEY_HEADER] == KEY for r in seen)


def test_stream_recording_stops_past_the_size_limit() -> None:
    seen: list = []
    stream = gateway(_recording_server(b"x" * 5000, seen)).stream_recording(
        KEY, 3, max_bytes=4999
    )
    with pytest.raises(RecordingTooLarge) as caught:
        run(_collect(stream))
    assert caught.value.max_bytes == 4999


def test_stream_recording_without_a_master_is_a_404() -> None:
    seen: list = []
    stream = gateway(_recording_server(b"x", seen, master_status=404)).stream_recording(
        KEY, 3, max_bytes=10
    )
    with pytest.raises(GatewayError) as caught:
        run(_collect(stream))
    assert caught.value.status == 404
    assert [r.url.path for r in seen] == ["/recordings/3/master"]
