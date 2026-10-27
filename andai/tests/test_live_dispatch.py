"""The live harness's pure parts: env reading, redaction, transcript lag and frame handling.

`andai/scripts/live_dispatch.py` is a script, not a package module, so it is loaded from its path.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_dispatch.py"
_spec = importlib.util.spec_from_file_location("live_dispatch", SCRIPT)
assert _spec and _spec.loader
ld = importlib.util.module_from_spec(_spec)
sys.modules["live_dispatch"] = ld
_spec.loader.exec_module(ld)

OLLM_URL = "https://ollm.internal.example/prefix"
OLLM_KEY = "sk-ollm-secret-value-123"
ADMIN = "admin-token-0123456789abcdef"
API_KEY = "mk_live_abcdef0123456789"
MEET = "https://meet.google.com/abc-defg-hij?authuser=0&pli=1"
T0 = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)


def _emitter() -> tuple[ld.Emitter, io.StringIO, io.StringIO]:
    out, log = io.StringIO(), io.StringIO()
    redactor = ld.Redactor()
    redactor.add(ADMIN, "<admin-token>")
    redactor.add(API_KEY, "<api-key>")
    redactor.add_url(OLLM_URL, ld.ollm_tag(OLLM_URL))
    redactor.add_url(MEET, ld.redact_url(MEET), host=False)
    redactor.add("abc-defg-hij", ld.redact_code("abc-defg-hij"))
    return ld.Emitter(redactor, out=out, log=log), out, log


def _records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


# ── env ──────────────────────────────────────────────────────────────────────


def test_read_env_keeps_only_the_harness_keys_and_never_the_ollm_key(tmp_path: Path) -> None:
    env = tmp_path / "env.live"
    env.write_text(
        "# comment\n"
        f"ADMIN_TOKEN={ADMIN}\n"
        "API_GATEWAY_HOST_PORT=18056\n"
        f'TRANSCRIPTION_SERVICE_URL="{OLLM_URL}"\n'
        f"TRANSCRIPTION_SERVICE_TOKEN={OLLM_KEY}\n"
        f"ANDAI_AGENT_API_KEY={OLLM_KEY}\n"
        "ADMIN_API_PORT=1\n"
        "ADMIN_API_PORT=18057\n"
    )

    values = ld.read_env(env)

    assert values == {
        "ADMIN_TOKEN": ADMIN,
        "API_GATEWAY_HOST_PORT": "18056",
        "TRANSCRIPTION_SERVICE_URL": OLLM_URL,
        "ADMIN_API_PORT": "18057",
    }
    assert OLLM_KEY not in repr(values)


# ── redaction ────────────────────────────────────────────────────────────────


def test_ollm_tag_is_stable_short_and_hides_the_host() -> None:
    tag = ld.ollm_tag(OLLM_URL)

    assert tag == ld.ollm_tag(OLLM_URL + "/")
    assert tag.startswith("ollm:") and len(tag) == len("ollm:") + 8
    assert "ollm.internal" not in tag
    assert ld.ollm_tag("") == "ollm:unset"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (MEET, "https://meet.google.com/abc…"),
        ("https://teams.microsoft.com/meet/123456789?p=SECRET", "https://teams.microsoft.com/123…"),
        ("https://us05web.zoom.us/j/81234567890?pwd=abcDEF", "https://us05web.zoom.us/812…"),
        ("https://meet.google.com/", "https://meet.google.com/…"),
    ],
)
def test_redact_url_keeps_host_and_three_code_characters_and_drops_the_query(
    url: str, expected: str
) -> None:
    assert ld.redact_url(url) == expected


def test_redact_code_truncates_meeting_codes() -> None:
    assert ld.redact_code("abc-defg-hij") == "abc…"
    assert ld.redact_code("ab") == "…"
    assert ld.redact_code(None) == "…"


def test_emitted_lines_never_carry_a_secret() -> None:
    emitter, out, log = _emitter()

    emitter.emit(
        "setup_error",
        error=f"{ADMIN} {API_KEY} {OLLM_URL}/v1/audio ollm.internal.example {MEET} abc-defg-hij",
    )

    for stream in (out, log):
        text = stream.getvalue()
        for secret in (ADMIN, API_KEY, "ollm.internal.example", "defg-hij", "authuser"):
            assert secret not in text
        error = _records(stream)[0]["error"]
        assert "<admin-token>" in error and "<api-key>" in error
        assert ld.ollm_tag(OLLM_URL) in error and "https://meet.google.com/abc…" in error


# ── lag ──────────────────────────────────────────────────────────────────────


def test_segment_lag_uses_absolute_end_time() -> None:
    seg = {"absolute_end_time": "2026-10-26T12:00:00.000Z", "end": 3.0}

    assert ld.segment_lag(seg, T0 + timedelta(seconds=2.345)) == 2.35
    assert ld.segment_lag({"absolute_end_time": "2026-10-26T17:30:00+05:30"}, T0) == 0.0


def test_segment_lag_falls_back_to_an_epoch_end_but_not_a_relative_offset() -> None:
    epoch = {"end": T0.timestamp()}

    assert ld.segment_lag(epoch, T0 + timedelta(seconds=1.5)) == 1.5
    assert ld.segment_lag({"end": 12.5}, T0) is None
    assert ld.segment_lag({"absolute_end_time": "not a time"}, T0) is None
    assert ld.segment_lag({}, T0) is None


def test_lag_stats() -> None:
    assert ld.lag_stats([]) == {"count": 0, "first": None, "median": None, "p95": None, "max": None}
    lags = [3.0, 1.0, 2.0, 10.0]
    assert ld.lag_stats(lags) == {"count": 4, "first": 3.0, "median": 2.5, "p95": 10.0, "max": 10.0}
    many = [float(i) for i in range(1, 101)]
    assert ld.lag_stats(many)["p95"] == 95.0


# ── frames ───────────────────────────────────────────────────────────────────


def _segment(sid: str, end: datetime, text: str = "hello there") -> dict:
    return {"segment_id": sid, "text": text, "language": "en", "speaker": "CTO",
            "absolute_end_time": end.isoformat().replace("+00:00", "Z")}


def test_watch_prints_each_status_change_once_and_each_segment_once() -> None:
    emitter, out, _ = _emitter()
    watch = ld.Watch(emitter)

    watch.on_meeting({"status": "requested"}, T0, source="dispatch")
    watch.on_frame({"type": "meeting.status", "payload": {"status": "joining"}}, T0)
    watch.on_meeting({"status": "joining"}, T0)  # the poll sees what the WS already said
    watch.on_frame({"type": "meeting.status", "payload": {"status": "active"}}, T0)
    draft = _segment("s1", T0)
    watch.on_frame({"type": "transcript", "confirmed": [], "pending": [draft]},
                   T0 + timedelta(seconds=1))
    final = _segment("s1", T0 + timedelta(seconds=2))
    for _ in range(2):  # a mutable update re-sends confirmed segments
        watch.on_frame({"type": "transcript", "confirmed": [final], "pending": []},
                       T0 + timedelta(seconds=5))
    watch.on_meeting({"status": "failed", "data": {"completion_reason": "stopped",
                                                   "failure_stage": "active"}}, T0)

    records = _records(out)
    assert [(r["event"], r.get("status")) for r in records] == [
        ("status", "requested"), ("status", "joining"), ("status", "active"),
        ("draft", None), ("segment", None), ("status", "failed"),
    ]
    assert records[0]["source"] == "dispatch" and records[1]["source"] == "ws"
    assert records[4]["lag_s"] == 3.0 and records[4]["text"] == "hello there"
    assert records[5]["completion_reason"] == "stopped"
    assert '"status": "active"' in out.getvalue()
    summary = watch.summary()
    assert summary["reached_active"] is True and summary["final_status"] == "failed"
    assert summary["segments"] == 1 and summary["drafts"] == 1
    assert summary["first_segment_after_active_s"] == 5.0
    assert summary["lag_s"]["first"] == 3.0
    assert summary["completion_reason"] == "stopped"
    assert watch.terminal


def test_a_poll_older_than_the_last_status_change_does_not_move_the_status_back() -> None:
    emitter, out, _ = _emitter()
    watch = ld.Watch(emitter)
    watch.on_frame({"type": "meeting.status", "payload": {"status": "active"}}, T0)
    asked = T0 + timedelta(seconds=1)
    watch.on_frame({"type": "meeting.status", "payload": {"status": "stopping"}},
                   T0 + timedelta(seconds=2))

    watch.on_meeting({"status": "active"}, T0 + timedelta(seconds=3), asked=asked)
    assert watch.status == "stopping"
    watch.on_meeting({"status": "completed", "data": {"completion_reason": "stopped"}},
                     T0 + timedelta(seconds=5), asked=T0 + timedelta(seconds=4))
    watch.on_frame({"type": "meeting.status", "payload": {"status": "active"}},
                   T0 + timedelta(seconds=6))  # nothing follows a terminal status

    assert [r["status"] for r in _records(out)] == ["active", "stopping", "completed"]
    assert watch.status == "completed"


def test_watch_truncates_long_text_and_reports_ws_errors() -> None:
    emitter, out, _ = _emitter()
    watch = ld.Watch(emitter)

    watch.on_segments([_segment("s1", T0, text="x" * 500)], [], T0)
    watch.on_frame({"type": "error", "error": "forbidden"}, T0)

    records = _records(out)
    assert len(records[0]["text"]) == ld.TEXT_LIMIT + 1 and records[0]["text"].endswith("…")
    assert records[1] == {"ts": records[1]["ts"], "event": "ws_error", "error": "forbidden"}
    assert watch.summary()["reached_active"] is False


def test_watch_prints_the_disclosure_outcome_once_with_its_delay_after_admission() -> None:
    emitter, out, _ = _emitter()
    watch = ld.Watch(emitter)
    watch.on_frame({"type": "meeting.status", "payload": {"status": "active"}}, T0)
    outcome = {"channel": "chat", "posted_at": "2026-10-26T12:00:06.500Z"}
    for _ in range(2):  # every later poll carries it again
        watch.on_meeting({"status": "active", "data": {"disclosure": outcome}}, T0 + timedelta(seconds=10))

    records = [r for r in _records(out) if r["event"] == "disclosure"]
    assert records == [{"ts": records[0]["ts"], "event": "disclosure", "channel": "chat",
                        "posted_at": "2026-10-26T12:00:06.500Z", "posted_after_active_s": 6.5}]
    assert '"channel": "chat"' in out.getvalue()
    assert watch.summary()["disclosure"] == outcome


def test_watch_reports_a_name_only_outcome_with_its_reason() -> None:
    emitter, out, _ = _emitter()
    watch = ld.Watch(emitter)
    watch.on_meeting({"status": "active", "data": {"disclosure": {
        "channel": "bot_name_only", "detail": "composer_disabled"}}}, T0)
    record = [r for r in _records(out) if r["event"] == "disclosure"][0]
    assert record["channel"] == "bot_name_only" and record["detail"] == "composer_disabled"
    assert record["posted_after_active_s"] is None


def test_disclosure_flag_defaults_the_text_and_needs_a_privacy_link() -> None:
    args = ld.parse_args(["--url", MEET, "--disclosure", "--privacy-url", "https://example.com/p"])
    assert args.disclosure == ld.DEFAULT_DISCLOSURE and args.privacy_url == "https://example.com/p"
    assert len(ld.DEFAULT_DISCLOSURE) + len(" https://example.com/p") <= 300
    custom = ld.parse_args(["--url", MEET, "--disclosure", "Recording.", "--privacy-url", "http://x.example/p"])
    assert custom.disclosure == "Recording."
    assert ld.parse_args(["--url", MEET]).disclosure is None
    for bad in (["--disclosure"], ["--privacy-url", "https://example.com/p"],
                ["--disclosure", "--privacy-url", "ftp://example.com/p"]):
        with pytest.raises(SystemExit):
            ld.parse_args(["--url", MEET, *bad])


# ── entry point ──────────────────────────────────────────────────────────────


def test_setup_error_for_placeholder_env_is_redacted_and_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / "env.live"
    env.write_text(f"ADMIN_TOKEN={ADMIN}\nTRANSCRIPTION_SERVICE_URL=__OLLM_BASE_URL__\n")
    log = tmp_path / "logs" / "meet.log"

    code = ld.main(["--env-file", str(env), "--url", MEET, "--log", str(log)])

    assert code == 2
    out = capsys.readouterr().out
    assert ADMIN not in out and "defg" not in out
    events = [json.loads(line)["event"] for line in out.splitlines()]
    assert events == ["setup_error"]
    assert log.read_text() == out
