r"""Load driver: N concurrent mock meetings through andai-core, against a test stack only.

    python -m andai_core.testing.load \
        --base http://127.0.0.1:47300 --origin http://127.0.0.1:43100 \
        --email load@example.test --password-file /abs/pw --org-cap 50 \
        --n 8 --cap 5 --hold-s 180 --report /abs/report.json [--threshold 0.95]

It signs in as a member of the load org (2FA off for that org), checks the org's bot cap is at
least N (else exit 2: the org cap would refuse first and be mistaken for cluster capacity),
dispatches N Zoom-shaped links that select the mock bot's `hold` scenario
(`?mock=hold&hold_s=S`, each a unique meeting number) with bounded concurrency, and polls every
meeting (`GET /api/v1/meetings/{id}`, andai-core's own rows, not the gateway) every `--poll-s`
seconds until each is `recording` or terminal. The number of meetings in `recording` is sampled at
each poll, so the peak is concurrent at one instant, never a running total. Then it stops every
meeting and waits until all are terminal.

The report (stdout and `--report`) holds counts, ratios and timings only: no links, meeting ids,
emails or passwords. `ratio` is peak_recording / min(n, cap). The run passes when the ratio is at
least the threshold, every refusal is `capacity_exhausted` within `--refuse-deadline-s` of its
dispatch, the refusals match the dispatches over the cap (each other failure may free one slot),
and every meeting is terminal after the stop. Exit 0 pass, 1 fail, 2 setup error.

Only for a test stack with the mock bot (andai/docs/load-test.md); never a live stack.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import secrets
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

TERMINAL = frozenset({"done", "failed", "cancelled"})
RECORDING = "recording"
CAPACITY = "capacity_exhausted"
HOLD_MIN_S, HOLD_MAX_S = 60, 7200
PLATFORMS = ("zoom",)


class SetupError(Exception):
    """The run cannot start (sign-in, 2FA, the org cap); exit 2."""


@dataclass
class Config:
    n: int
    cap: int
    org_cap: int
    hold_s: int
    platform: str = "zoom"
    threshold: float = 0.95
    concurrency: int = 20
    poll_s: float = 2.0
    join_timeout_s: float = 180.0
    observe_s: float = 10.0
    stop_timeout_s: float = 180.0
    refuse_deadline_s: float = 10.0


@dataclass
class Track:
    """One dispatched meeting. `id` never leaves the process."""

    dispatched_at: float
    id: str | None = None
    status: str | None = None
    failure_reason: str | None = None
    http_status: int | None = None
    recording_at: float | None = None
    terminal_at: float | None = None

    @property
    def terminal(self) -> bool:
        return self.http_status is not None or self.status in TERMINAL

    @property
    def refused(self) -> bool:
        return self.status == "failed" and self.failure_reason == CAPACITY


@dataclass
class Run:
    tracks: list[Track] = field(default_factory=list)
    samples: list[int] = field(default_factory=list)  # meetings in `recording`, per poll
    terminal_after_stop: Counter = field(default_factory=Counter)
    all_terminal_after_stop: bool = False


def link(platform: str, hold_s: int) -> str:
    """A unique Zoom-shaped meeting link that plays the mock bot's `hold` for `hold_s` seconds."""
    if platform != "zoom":
        raise SetupError(f"platform {platform!r} is not supported; one of {PLATFORMS}")
    number = 10_000_000_000 + secrets.randbelow(89_999_999_999)
    return f"https://zoom.us/j/{number}?pwd=loadtest&mock=hold&hold_s={hold_s}"


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _failure(t: Track) -> str:
    """Why a meeting that never reached `recording` ended: the HTTP status of a refused join, its
    failure reason, or `<status>_before_recording` (stopped or ended while still joining)."""
    if t.http_status is not None:
        return f"http_{t.http_status}"
    return t.failure_reason or f"{t.status or 'unknown'}_before_recording"


def build_report(run: Run, cfg: Config) -> dict:
    """The run's numbers and verdict. Counts and timings only."""
    expected = min(cfg.n, cfg.cap)
    over_cap = max(0, cfg.n - cfg.cap)
    peak = max(run.samples, default=0)
    ratio = peak / expected if expected else 0.0
    latencies = [t.recording_at - t.dispatched_at for t in run.tracks if t.recording_at is not None]
    refusals = [t for t in run.tracks if t.refused]
    refused_within = [
        t.terminal_at - t.dispatched_at for t in refusals if t.terminal_at is not None
    ]
    other = Counter(
        _failure(t) for t in run.tracks if t.terminal and not t.refused and t.recording_at is None
    )
    other_total = sum(other.values())
    problems = []
    if ratio < cfg.threshold:
        problems.append(f"peak_recording {peak} is below {cfg.threshold:.0%} of {expected}")
    if len(refusals) > over_cap:
        problems.append(f"{len(refusals)} refused but only {over_cap} dispatches were over the cap")
    if len(refusals) < over_cap - other_total:
        problems.append(f"{len(refusals)} refused as {CAPACITY}, {over_cap} were over the cap")
    if refused_within and max(refused_within) > cfg.refuse_deadline_s:
        problems.append(f"a refusal took longer than {cfg.refuse_deadline_s:g} s")
    if not run.all_terminal_after_stop:
        problems.append("not every meeting was terminal after the stop")

    def seconds(value: float | None) -> float | None:
        return None if value is None else round(value, 2)

    return {
        "n": cfg.n,
        "cap": cfg.cap,
        "org_cap": cfg.org_cap,
        "platform": cfg.platform,
        "hold_s": cfg.hold_s,
        "dispatched": len(run.tracks),
        "expected_recording": expected,
        "peak_recording": peak,
        "ratio": round(ratio, 4),
        "threshold": cfg.threshold,
        "reached_recording": len(latencies),
        "latency_to_recording_s": {
            "p50": seconds(percentile(latencies, 0.50)),
            "p95": seconds(percentile(latencies, 0.95)),
            "max": seconds(max(latencies, default=None)),
        },
        "refused_capacity": len(refusals),
        "refused_within_s_max": seconds(max(refused_within, default=None)),
        "other_failures": dict(sorted(other.items())),
        "all_terminal_after_stop": run.all_terminal_after_stop,
        "terminal_after_stop": dict(sorted(run.terminal_after_stop.items())),
        "samples": len(run.samples),
        "passed": not problems,
        "problems": problems,
    }


class Driver:
    """Drives one run over an httpx client already pointed at andai-core (base URL, Origin)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        cfg: Config,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        log: Callable[[str], None] = lambda m: print(m, file=sys.stderr, flush=True),
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.run_state = Run()
        self._gate = asyncio.Semaphore(cfg.concurrency)

    async def sign_in(self, email: str, password: str) -> None:
        r = await self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        if r.status_code != 200:
            raise SetupError(f"sign-in refused ({r.status_code})")
        stage = r.json().get("stage")
        if stage != "full":
            raise SetupError(f"sign-in stage is {stage!r}; the load org needs 2FA off")

    def preflight(self) -> None:
        cfg = self.cfg
        if not HOLD_MIN_S <= cfg.hold_s <= HOLD_MAX_S:
            raise SetupError(f"hold_s must be {HOLD_MIN_S}-{HOLD_MAX_S}, got {cfg.hold_s}")
        if cfg.n < 1 or cfg.cap < 1:
            raise SetupError("n and cap must be at least 1")
        if cfg.platform not in PLATFORMS:
            raise SetupError(f"platform {cfg.platform!r} is not supported; one of {PLATFORMS}")
        if cfg.org_cap < cfg.n:
            raise SetupError(
                f"the org's bot cap is {cfg.org_cap}, below n={cfg.n}: it would refuse first. "
                "Raise ANDAI_ORG_MAX_CONCURRENT_BOTS and run `make -C andai principals-sync`."
            )

    async def _dispatch(self, track: Track) -> None:
        async with self._gate:
            track.dispatched_at = self.clock()
            r = await self.client.post(
                "/api/v1/meetings/join", json={"join_url": link(self.cfg.platform, self.cfg.hold_s)}
            )
        if r.status_code not in (200, 201):
            track.http_status = r.status_code
            track.terminal_at = self.clock()
            return
        self._observe(track, r.json())

    def _observe(self, track: Track, meeting: dict) -> None:
        track.id = meeting.get("id", track.id)
        track.status = meeting.get("status")
        track.failure_reason = meeting.get("failure_reason")
        now = self.clock()
        if track.status == RECORDING and track.recording_at is None:
            track.recording_at = now
        if track.status in TERMINAL and track.terminal_at is None:
            track.terminal_at = now

    async def _poll(self, track: Track) -> None:
        if track.id is None or track.http_status is not None:
            return
        async with self._gate:
            r = await self.client.get(f"/api/v1/meetings/{track.id}")
        if r.status_code == 200:
            self._observe(track, r.json())

    async def _poll_round(self, tracks: list[Track]) -> None:
        await asyncio.gather(*(self._poll(t) for t in tracks))
        self.run_state.samples.append(
            sum(1 for t in self.run_state.tracks if t.status == RECORDING)
        )

    async def dispatch_and_watch(self) -> None:
        cfg = self.cfg
        tracks = [Track(dispatched_at=self.clock()) for _ in range(cfg.n)]
        self.run_state.tracks = tracks
        self.log(f"load: dispatching {cfg.n} mock meetings (cap {cfg.cap})")
        await asyncio.gather(*(self._dispatch(t) for t in tracks))
        self.run_state.samples.append(sum(1 for t in tracks if t.status == RECORDING))
        deadline = self.clock() + cfg.join_timeout_s
        while self.clock() < deadline:
            waiting = [t for t in tracks if t.recording_at is None and not t.terminal]
            if not waiting:
                break
            await self.sleep(cfg.poll_s)
            await self._poll_round([t for t in tracks if not t.terminal])
        recording = sum(1 for t in tracks if t.status == RECORDING)
        self.log(f"load: {recording} recording; observing for {cfg.observe_s:g} s")
        until = self.clock() + cfg.observe_s
        while self.clock() < until:
            await self.sleep(cfg.poll_s)
            await self._poll_round([t for t in tracks if not t.terminal])

    async def stop_all(self) -> None:
        cfg = self.cfg
        live = [t for t in self.run_state.tracks if t.id is not None and not t.terminal]
        self.log(f"load: stopping {len(live)} meetings")

        async def stop(track: Track) -> None:
            async with self._gate:
                await self.client.post(f"/api/v1/meetings/{track.id}/stop")

        await asyncio.gather(*(stop(t) for t in live))
        deadline = self.clock() + cfg.stop_timeout_s
        while self.clock() < deadline and any(not t.terminal for t in self.run_state.tracks):
            await self.sleep(cfg.poll_s)
            await asyncio.gather(*(self._poll(t) for t in self.run_state.tracks if not t.terminal))
        tracks = self.run_state.tracks
        self.run_state.all_terminal_after_stop = all(t.terminal for t in tracks)
        self.run_state.terminal_after_stop = Counter(
            f"http_{t.http_status}" if t.http_status is not None else (t.status or "unknown")
            for t in tracks
        )

    async def run(self, email: str, password: str) -> dict:
        self.preflight()
        await self.sign_in(email, password)
        try:
            await self.dispatch_and_watch()
        finally:
            await self.stop_all()
        return build_report(self.run_state, self.cfg)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m andai_core.testing.load", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", required=True, help="andai-core base URL, e.g. http://127.0.0.1:47300")
    p.add_argument("--origin", required=True,
                   help="the portal origin andai-core accepts writes from")
    p.add_argument("--email", required=True)
    p.add_argument("--password-file", required=True, type=Path)
    p.add_argument("--org-cap", required=True, type=int,
                   help="the load org's bot cap on the platform")
    p.add_argument("--n", required=True, type=int, help="meetings to dispatch")
    p.add_argument("--cap", required=True, type=int, help="RUNTIME_MAX_ACTIVE_BOTS of the stack")
    p.add_argument("--hold-s", required=True, type=int)
    p.add_argument("--platform", default="zoom", choices=PLATFORMS)
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--threshold", type=float, default=0.95)
    p.add_argument("--concurrency", type=int, default=20)
    p.add_argument("--poll-s", type=float, default=2.0)
    p.add_argument("--join-timeout-s", type=float, default=180.0)
    p.add_argument("--observe-s", type=float, default=10.0)
    p.add_argument("--stop-timeout-s", type=float, default=180.0)
    return p.parse_args(argv)


async def _main(a: argparse.Namespace) -> int:
    cfg = Config(n=a.n, cap=a.cap, org_cap=a.org_cap, hold_s=a.hold_s, platform=a.platform,
                 threshold=a.threshold, concurrency=a.concurrency, poll_s=a.poll_s,
                 join_timeout_s=a.join_timeout_s, observe_s=a.observe_s,
                 stop_timeout_s=a.stop_timeout_s)
    try:
        password = a.password_file.read_text().strip()
    except OSError as exc:
        raise SetupError(f"cannot read the password file ({exc.strerror})") from exc
    started = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()
    async with httpx.AsyncClient(base_url=a.base, headers={"Origin": a.origin},
                                 timeout=60.0) as client:
        report = await Driver(client, cfg).run(a.email, password)
    report = {"started_at": started, "duration_s": round(time.monotonic() - t0, 1), **report}
    text = json.dumps(report, indent=2)
    a.report.write_text(text + "\n")
    print(text)
    return 0 if report["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except SetupError as exc:
        print(f"load: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
