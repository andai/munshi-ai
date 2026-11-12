"""The load driver (andai_core.testing.load): its report maths, and a whole run against a fake
andai-core that admits `cap` meetings and refuses the rest as `capacity_exhausted`, on a fake clock.
No stack, no network."""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from andai_core.testing import load
from andai_core.testing.load import Config, Driver, Run, SetupError, Track, build_report

ORIGIN = "http://127.0.0.1:43100"
EMAIL = "load-member@example.test"


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class FakeAndai:
    """andai-core's join/get/stop as the driver sees them, on the fake clock. The first `cap` joins
    are admitted: `dispatching`, then `recording` `join_s` (+ `stagger_s` per earlier meeting) after
    the join, and with `live_s` set `done` that long after, like the old mock that ended in
    seconds. Joins over the cap answer `failed`/`capacity_exhausted` at once. A stop ends a meeting
    on its next read."""

    def __init__(self, cap: int, *, join_s: float = 4.0, stagger_s: float = 0.0,
                 live_s: float | None = None, stage: str = "full") -> None:
        self.t = FakeTime()
        self.cap = cap
        self.join_s = join_s
        self.stagger_s = stagger_s
        self.live_s = live_s
        self.stage = stage
        self.meetings: dict[str, dict] = {}
        self.links: list[str] = []
        self.writes_without_origin = 0
        self.unauthenticated = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method != "GET" and request.headers.get("origin") != ORIGIN:
            self.writes_without_origin += 1
            return httpx.Response(403, json={"detail": "origin"})
        if path == "/api/v1/auth/login":
            body = json.loads(request.content)
            assert body == {"email": EMAIL, "password": "pw-synthetic"}
            return httpx.Response(200, json={"stage": self.stage},
                                  headers={"set-cookie": "andai_session=s1; Path=/; HttpOnly"})
        if "andai_session=s1" not in request.headers.get("cookie", ""):
            self.unauthenticated += 1
            return httpx.Response(401, json={"detail": "not signed in"})
        if path == "/api/v1/meetings/join":
            url = json.loads(request.content)["join_url"]
            self.links.append(url)
            admitted = sum(1 for m in self.meetings.values() if m["admitted"])
            meeting = {"id": str(uuid.uuid4()), "created": self.t.now, "idx": admitted,
                       "admitted": admitted < self.cap, "stopped": False,
                       "status": "dispatching", "failure_reason": None}
            if not meeting["admitted"]:
                meeting.update(status="failed", failure_reason="capacity_exhausted")
            self.meetings[meeting["id"]] = meeting
            return httpx.Response(201, json=self._view(meeting))
        meeting_id = path.split("/")[4]
        meeting = self.meetings[meeting_id]
        if path.endswith("/stop"):
            meeting["stopped"] = True
            return httpx.Response(200, json=self._view(meeting))
        self._advance(meeting)
        return httpx.Response(200, json=self._view(meeting))

    def _advance(self, m: dict) -> None:
        if m["status"] in ("done", "failed"):
            return
        elapsed = self.t.now - m["created"]
        start = self.join_s + m["idx"] * self.stagger_s
        if m["stopped"]:
            m["status"] = "done"
        elif elapsed >= start:
            ended = self.live_s is not None and elapsed >= start + self.live_s
            m["status"] = "done" if ended else "recording"

    @staticmethod
    def _view(m: dict) -> dict:
        return {"id": m["id"], "status": m["status"], "failure_reason": m["failure_reason"]}


def drive(cfg: Config, andai: FakeAndai) -> dict:
    t = andai.t

    async def go() -> dict:
        transport = httpx.MockTransport(andai.handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://andai.test",
                                     headers={"Origin": ORIGIN}) as client:
            driver = Driver(client, cfg, clock=t.clock, sleep=t.sleep, log=lambda m: None)
            return await driver.run(EMAIL, "pw-synthetic")

    return asyncio.run(go())


def config(**kw) -> Config:
    base = dict(n=8, cap=5, org_cap=50, hold_s=180, poll_s=2.0, join_timeout_s=60.0,
                observe_s=6.0, stop_timeout_s=60.0)
    return Config(**{**base, **kw})


# ── a whole run ───────────────────────────────────────────────────────────────────────────────────

def test_eight_against_a_cap_of_five_is_five_recording_and_three_refused():
    andai = FakeAndai(cap=5)
    report = drive(config(), andai)

    assert report["peak_recording"] == 5
    assert report["expected_recording"] == 5 and report["ratio"] == 1.0
    assert report["refused_capacity"] == 3
    assert report["refused_within_s_max"] == 0.0             # refused in the join answer
    assert report["reached_recording"] == 5
    assert report["latency_to_recording_s"] == {"p50": 4.0, "p95": 4.0, "max": 4.0}
    assert report["other_failures"] == {}
    assert report["all_terminal_after_stop"] is True
    assert report["terminal_after_stop"] == {"done": 5, "failed": 3}
    assert report["passed"] is True and report["problems"] == []
    assert andai.writes_without_origin == 0 and andai.unauthenticated == 0


def test_every_link_is_a_unique_zoom_link_that_plays_hold():
    andai = FakeAndai(cap=5)
    drive(config(hold_s=240), andai)
    assert len(set(andai.links)) == 8
    for url in andai.links:
        assert url.startswith("https://zoom.us/j/") and url.endswith("&mock=hold&hold_s=240")


def test_the_report_names_no_link_meeting_or_person():
    andai = FakeAndai(cap=5)
    text = json.dumps(drive(config(), andai))
    assert "zoom" not in text.replace('"platform": "zoom"', "")
    assert EMAIL not in text and "pw-synthetic" not in text
    assert not any(meeting_id in text for meeting_id in andai.meetings)


def test_the_peak_is_concurrent_not_cumulative():
    """Meetings that end 3 s after reaching `recording`, each 4 s after the one before (the old
    mock's short meetings): all five reach it, but never at the same instant, so the run fails on
    its peak instead of counting them up."""
    staggered = drive(config(n=5, cap=5),
                      FakeAndai(cap=5, join_s=2.0, stagger_s=4.0, live_s=3.0))
    assert staggered["reached_recording"] == 5
    assert staggered["peak_recording"] == 1
    assert staggered["passed"] is False
    assert any("peak_recording" in p for p in staggered["problems"])


def test_under_the_cap_nothing_is_refused():
    report = drive(config(n=4, cap=5), FakeAndai(cap=5))
    assert (report["peak_recording"], report["expected_recording"]) == (4, 4)
    assert report["refused_capacity"] == 0 and report["passed"] is True


def test_a_cap_that_trips_early_fails_the_run():
    """The fake admits 3 although the stack says 5: the extra refusals are the finding."""
    report = drive(config(), FakeAndai(cap=3))
    assert report["refused_capacity"] == 5
    assert report["passed"] is False


# ── setup errors: exit 2 ──────────────────────────────────────────────────────────────────────────

def test_an_org_cap_below_n_stops_before_any_dispatch():
    andai = FakeAndai(cap=5)
    with pytest.raises(SetupError, match="bot cap is 3, below n=8"):
        drive(config(org_cap=3), andai)
    assert andai.links == []


@pytest.mark.parametrize("stage", ["enroll_required", "mfa_pending"])
def test_a_load_org_with_2fa_is_refused(stage):
    with pytest.raises(SetupError, match="2FA off"):
        drive(config(), FakeAndai(cap=5, stage=stage))


@pytest.mark.parametrize("hold_s", [59, 7201])
def test_hold_outside_the_mock_range_is_refused(hold_s):
    with pytest.raises(SetupError, match="hold_s"):
        drive(config(hold_s=hold_s), FakeAndai(cap=5))


# ── the report maths on fixed tracks ──────────────────────────────────────────────────────────────

def track(*, latency=None, status="done", reason=None, http=None, refused_after=None) -> Track:
    t = Track(dispatched_at=100.0, status=status, failure_reason=reason, http_status=http)
    if latency is not None:
        t.recording_at = 100.0 + latency
    if refused_after is not None:
        t.status, t.failure_reason = "failed", "capacity_exhausted"
        t.terminal_at = 100.0 + refused_after
    return t


def test_report_maths():
    tracks = [track(latency=float(s)) for s in range(1, 21)]          # 20 admitted, 1..20 s
    tracks += [track(refused_after=0.4), track(refused_after=3.2)]    # 2 over the cap
    tracks += [track(status="failed", reason="admission_timeout"), track(http=502)]
    run = Run(tracks=tracks, samples=[0, 7, 19, 18], all_terminal_after_stop=True)
    report = build_report(run, Config(n=24, cap=20, org_cap=50, hold_s=600, threshold=0.95))

    assert report["peak_recording"] == 19
    assert report["ratio"] == 0.95
    assert report["latency_to_recording_s"] == {"p50": 10.0, "p95": 19.0, "max": 20.0}
    assert report["refused_capacity"] == 2 and report["refused_within_s_max"] == 3.2
    assert report["other_failures"] == {"admission_timeout": 1, "http_502": 1}
    assert report["passed"] is True                    # 19/20 meets 95 %; 2 failures freed slots


def test_report_fails_below_the_threshold_and_on_a_slow_refusal():
    tracks = [track(latency=1.0) for _ in range(10)] + [track(refused_after=12.5)]
    run = Run(tracks=tracks, samples=[9], all_terminal_after_stop=True)
    report = build_report(run, Config(n=11, cap=10, org_cap=50, hold_s=600, threshold=0.95))
    assert report["ratio"] == 0.9
    assert report["passed"] is False
    assert any("below 95%" in p for p in report["problems"])
    assert any("longer than 10 s" in p for p in report["problems"])


def test_report_fails_when_meetings_are_left_running():
    run = Run(tracks=[track(latency=1.0)], samples=[1], all_terminal_after_stop=False)
    report = build_report(run, Config(n=1, cap=1, org_cap=5, hold_s=600))
    assert report["passed"] is False
    assert report["problems"] == ["not every meeting was terminal after the stop"]


def test_percentile_is_nearest_rank():
    assert load.percentile([], 0.5) is None
    assert load.percentile([3.0], 0.95) == 3.0
    assert load.percentile([float(v) for v in range(1, 101)], 0.95) == 95.0


# ── the command line: exit codes ──────────────────────────────────────────────────────────────────

def cli(tmp_path, monkeypatch, andai: FakeAndai, *extra: str) -> tuple[int, dict | None]:
    real = httpx.AsyncClient

    def client(**kw):
        return real(transport=httpx.MockTransport(andai.handler), **kw)

    monkeypatch.setattr(load.httpx, "AsyncClient", client)
    pw = tmp_path / "pw"
    pw.write_text("pw-synthetic\n")
    out = tmp_path / "report.json"
    code = load.main([
        "--base", "http://andai.test", "--origin", ORIGIN, "--email", EMAIL,
        "--password-file", str(pw), "--n", "8", "--cap", "5", "--hold-s", "180",
        "--report", str(out), "--poll-s", "0.01", "--observe-s", "0.05", *extra,
    ])
    return code, (json.loads(out.read_text()) if out.exists() else None)


def test_cli_exit_0_on_a_passing_run(tmp_path, monkeypatch):
    code, report = cli(tmp_path, monkeypatch, FakeAndai(cap=5, join_s=0.0), "--org-cap", "50")
    assert code == 0 and report["passed"] is True and report["peak_recording"] == 5


def test_cli_exit_1_below_the_threshold(tmp_path, monkeypatch):
    code, report = cli(tmp_path, monkeypatch, FakeAndai(cap=3, join_s=0.0), "--org-cap", "50")
    assert code == 1 and report["passed"] is False


def test_cli_exit_2_on_an_org_cap_below_n(tmp_path, monkeypatch, capsys):
    code, report = cli(tmp_path, monkeypatch, FakeAndai(cap=5), "--org-cap", "3")
    assert code == 2 and report is None
    assert "bot cap is 3" in capsys.readouterr().err
