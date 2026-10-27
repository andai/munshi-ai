#!/usr/bin/env python3
"""live_dispatch.py — send the real meeting bot into a live test meeting and watch it.

The operator's live harness (andai/docs/live-test.md). Against a running stack on this machine it:

1. ensures the operator test principal `live-test@principals.munshi.invalid` on admin-api (diagnostic
   signal tapes off), mints a `bot,tx` key for this run and revokes it on exit;
2. dispatches the bot through the gateway (`POST /bots` with `meeting_url`, `language`, the
   recorder bot name and, with `--disclosure`, the recording notice the bot posts in meeting chat);
3. watches the meeting on the gateway's `/ws` feed (status frames and live transcript frames) and by
   polling `GET /meetings/{id}`, printing every status change with a UTC timestamp, every
   transcript segment with its lag (arrival time minus the segment's `absolute_end_time`) and the
   bot's reported chat-disclosure outcome (`meeting.data.disclosure`);
4. on Ctrl-C, `--stop-after` or a terminal status, stops the bot (`DELETE /bots/...`), waits for the
   meeting to end and prints a summary with the lag statistics.

Output is one JSON object per line on stdout (and appended to `--log`). Every line passes through a
redactor: the admin token, the run's API key, the OLLM URL and host, the meeting URL and the full
meeting code never appear. The STT route is printed as `ollm:<hash8>` and meeting codes as their
first three characters. Only the env keys this harness needs are read; the OLLM key is never read.

Exit status: 0 when the bot reached `active`, 1 when it never did, 2 on a setup error.

    uv run --directory andai python scripts/live_dispatch.py \\
        --env-file "$PWD/.work/.local/env.live" --url 'https://meet.google.com/abc-defg-hij' --language en
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import signal
import statistics
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect as ws_connect

PRINCIPAL_EMAIL = "live-test@principals.munshi.invalid"
SCOPES = ("bot", "tx")
DEFAULT_BOT_NAME = "munshi.ai (recording)"
# The recording notice `--disclosure` posts when given no text (the privacy link is appended).
DEFAULT_DISCLOSURE = (
    "munshi.ai is recording and transcribing this meeting for the organiser. "
    "To stop it, remove the munshi.ai bot from the call. Privacy notice:"
)
HOST = "127.0.0.1"
TERMINAL = frozenset({"completed", "failed"})
# The env keys this harness reads; nothing else in the env file is kept in memory.
ENV_KEYS = (
    "ADMIN_TOKEN",
    "ADMIN_API_PORT",
    "API_GATEWAY_HOST_PORT",
    "TRANSCRIPTION_SERVICE_URL",
    "TRANSCRIPTION_MODEL",
)
# A segment time at or above this many seconds is an absolute epoch, below it an offset.
EPOCH_THRESHOLD_S = 1_000_000_000
POLL_SECONDS = 5.0
SUBSCRIBE_RETRY_SECONDS = 5.0
STOP_GRACE_SECONDS = 90.0
TEXT_LIMIT = 120


class SetupError(Exception):
    """The stack, the env file or the dispatch refused; the message is safe to print."""


# ── env ──────────────────────────────────────────────────────────────────────


def read_env(path: Path, keys: Iterable[str] = ENV_KEYS) -> dict[str, str]:
    """The last value of each of `keys` in the KEY=VALUE file `path` (quotes stripped). Other keys
    are skipped without being stored; the file is never sourced or executed."""
    wanted = set(keys)
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in wanted:
            values[key] = value.strip().strip("'\"")
    return values


# ── redaction ────────────────────────────────────────────────────────────────


def ollm_tag(url: str) -> str:
    """A stable, non-reversible label for the STT route: `ollm:<first 8 hex of sha256(url)>`."""
    url = url.strip().rstrip("/")
    if not url:
        return "ollm:unset"
    return "ollm:" + hashlib.sha256(url.encode()).hexdigest()[:8]


def redact_code(code: str | None) -> str:
    """A meeting code cut to its first three characters (`abc-defg-hij` → `abc…`)."""
    code = (code or "").strip()
    return f"{code[:3]}…" if len(code) > 3 else "…"


def redact_url(url: str) -> str:
    """A meeting URL as scheme://host/<first three characters of its last path segment>…, with
    no query or fragment (a passcode or tenant id never survives)."""
    parts = urlsplit(url.strip())
    segments = [s for s in parts.path.split("/") if s]
    tail = redact_code(segments[-1]) if segments else "…"
    host = parts.hostname or "?"
    return f"{parts.scheme or 'https'}://{host}/{tail}"


@dataclass
class Redactor:
    """Replaces every registered secret in a string with its label (longest secret first)."""

    _secrets: dict[str, str] = field(default_factory=dict, repr=False)

    def add(self, value: str | None, label: str) -> None:
        value = (value or "").strip()
        if len(value) >= 4:
            self._secrets[value] = label

    def add_url(self, url: str | None, label: str, *, host: bool = True) -> None:
        """Register a URL, its no-trailing-slash form and (with `host`) its host name."""
        url = (url or "").strip()
        if not url:
            return
        self.add(url, label)
        self.add(url.rstrip("/"), label)
        name = urlsplit(url).hostname if host else None
        if name and name not in (HOST, "localhost"):
            self.add(name, label)

    def scrub(self, text: str) -> str:
        for value in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(value, self._secrets[value])
        return text


# ── lag ──────────────────────────────────────────────────────────────────────


def parse_ts(value: object) -> datetime | None:
    """An aware UTC datetime from an ISO-8601 string (`Z` or an offset), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def segment_end(segment: dict) -> datetime | None:
    """When the segment's speech ended: its `absolute_end_time`, else its `end` when that is an
    absolute epoch (the live pipeline's shape), else None (a relative offset has no wall clock)."""
    end = parse_ts(segment.get("absolute_end_time"))
    if end is not None:
        return end
    raw = segment.get("end")
    try:
        seconds = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    if seconds is not None and seconds >= EPOCH_THRESHOLD_S:
        return datetime.fromtimestamp(seconds, UTC)
    return None


def segment_lag(segment: dict, arrived: datetime) -> float | None:
    """Seconds from the end of the segment's speech to its arrival here, rounded to 0.01 s."""
    end = segment_end(segment)
    if end is None:
        return None
    return round((arrived - end).total_seconds(), 2)


def lag_stats(lags: list[float]) -> dict[str, float | int | None]:
    """count, first, median, p95 and max of the lags in arrival order (None when empty)."""
    if not lags:
        return {"count": 0, "first": None, "median": None, "p95": None, "max": None}
    ordered = sorted(lags)
    p95 = ordered[min(len(ordered) - 1, max(0, round(0.95 * len(ordered)) - 1))]
    return {
        "count": len(lags),
        "first": lags[0],
        "median": round(statistics.median(ordered), 2),
        "p95": p95,
        "max": ordered[-1],
    }


# ── output ───────────────────────────────────────────────────────────────────


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Emitter:
    """Writes one redacted JSON object per line to stdout and, if given, the log file."""

    def __init__(self, redactor: Redactor, out: TextIO | None = None, log: TextIO | None = None):
        self.redactor = redactor
        self.out = out  # None: whatever sys.stdout is at write time
        self.log = log

    def emit(self, event: str, ts: datetime | None = None, **fields: object) -> dict:
        record = {"ts": iso(ts or utc_now()), "event": event, **fields}
        line = self.redactor.scrub(json.dumps(record, ensure_ascii=False, default=str))
        for stream in (self.out or sys.stdout, self.log):
            if stream is not None:
                stream.write(line + "\n")
                stream.flush()
        return record


# ── the meeting being watched ────────────────────────────────────────────────


@dataclass
class Watch:
    """What the harness has seen of one meeting; fed by WS frames and polls."""

    emitter: Emitter
    status: str | None = None
    reached_active: bool = False
    active_at: datetime | None = None
    lags: list[float] = field(default_factory=list)
    first_segment_after_active: float | None = None
    detail: dict[str, object] = field(default_factory=dict)
    disclosure: dict[str, object] | None = None
    changed_at: datetime | None = None
    _confirmed: set[str] = field(default_factory=set)
    _drafts: set[str] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def on_status(self, status: str, source: str, at: datetime, **detail: object) -> None:
        if not status or status == self.status or self.terminal:
            return
        self.status, self.changed_at = status, at
        if status == "active" and not self.reached_active:
            self.reached_active, self.active_at = True, at
        extra = {k: v for k, v in detail.items() if v not in (None, "")}
        self.emitter.emit("status", at, status=status, source=source, **extra)
        self.changed.set()

    def on_segments(self, confirmed: list, pending: list, at: datetime) -> None:
        for seg in confirmed:
            if isinstance(seg, dict) and seg.get("segment_id") not in self._confirmed:
                self._confirmed.add(seg.get("segment_id"))
                self._segment("segment", seg, at)
        for seg in pending:
            if isinstance(seg, dict) and seg.get("segment_id") not in self._drafts | self._confirmed:
                self._drafts.add(seg.get("segment_id"))
                self._segment("draft", seg, at)

    def _segment(self, kind: str, seg: dict, at: datetime) -> None:
        lag = segment_lag(seg, at)
        if kind == "segment" and lag is not None:
            self.lags.append(lag)
            if self.first_segment_after_active is None and self.active_at is not None:
                self.first_segment_after_active = round((at - self.active_at).total_seconds(), 2)
        text = (seg.get("text") or "").strip()
        self.emitter.emit(
            kind, at,
            lag_s=lag,
            language=seg.get("language"),
            speaker=seg.get("speaker"),
            text=text[:TEXT_LIMIT] + ("…" if len(text) > TEXT_LIMIT else ""),
        )

    def on_frame(self, frame: dict, at: datetime) -> None:
        kind = frame.get("type")
        if kind == "meeting.status":
            payload = frame.get("payload") or {}
            self.on_status(str(payload.get("status") or frame.get("status") or ""), "ws", at)
        elif kind == "transcript":
            self.on_segments(frame.get("confirmed") or [], frame.get("pending") or [], at)
        elif kind == "subscribed":
            self.emitter.emit("ws_subscribed", at)
        elif kind == "error":
            self.emitter.emit("ws_error", at, error=frame.get("error") or frame.get("detail"))

    def on_meeting(self, body: dict, at: datetime, source: str = "poll",
                   asked: datetime | None = None) -> None:
        """A meeting row read at `at` by a request sent at `asked`. A row asked for before the last
        status change is older than that change, so its status is dropped (its reasons are kept)."""
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        detail = {k: data.get(k) for k in ("completion_reason", "failure_stage") if data.get(k)}
        self.detail.update(detail)
        self.on_disclosure(data.get("disclosure"), at)
        if asked is not None and self.changed_at is not None and asked < self.changed_at:
            return
        self.on_status(str(body.get("status") or ""), source, at, **detail)

    def on_disclosure(self, outcome: object, at: datetime) -> None:
        """The bot's chat-disclosure outcome, printed once when it first appears on the meeting."""
        if self.disclosure is not None or not isinstance(outcome, dict) or not outcome.get("channel"):
            return
        self.disclosure = {k: outcome.get(k) for k in ("channel", "posted_at", "detail")
                           if outcome.get(k) is not None}
        posted = parse_ts(outcome.get("posted_at"))
        after = (round((posted - self.active_at).total_seconds(), 2)
                 if posted is not None and self.active_at is not None else None)
        self.emitter.emit("disclosure", at, **self.disclosure, posted_after_active_s=after)

    def summary(self) -> dict[str, object]:
        return {
            "final_status": self.status,
            **self.detail,
            "disclosure": self.disclosure,
            "reached_active": self.reached_active,
            "segments": len(self._confirmed),
            "drafts": len(self._drafts),
            "first_segment_after_active_s": self.first_segment_after_active,
            "lag_s": lag_stats(self.lags),
        }


# ── the stack ────────────────────────────────────────────────────────────────


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else json.dumps(detail)[:300] if detail else ""


async def ensure_principal(admin: httpx.AsyncClient) -> tuple[int, int, str]:
    """(user id, token id, token) of the operator test principal, with tapes off and a fresh key."""
    created = await admin.post("/admin/users", json={"email": PRINCIPAL_EMAIL})
    if created.status_code not in (200, 201):
        raise SetupError(f"admin-api create user answered {created.status_code}")
    user_id = created.json().get("id")
    if not isinstance(user_id, int):
        raise SetupError("admin-api create user answered without an id")
    patched = await admin.patch(
        f"/admin/users/{user_id}", json={"data": {"diagnostics": {"capture_signal": False}}}
    )
    if patched.status_code >= 400:
        raise SetupError(f"admin-api update user answered {patched.status_code}")
    minted = await admin.post(f"/admin/users/{user_id}/tokens", params={"scopes": ",".join(SCOPES)})
    if minted.status_code >= 400:
        raise SetupError(f"admin-api mint token answered {minted.status_code}")
    body = minted.json()
    token_id, token = body.get("id"), body.get("token")
    if not isinstance(token_id, int) or not isinstance(token, str) or not token:
        raise SetupError("admin-api mint token answered without a token")
    if sorted(body.get("scopes") or []) != sorted(SCOPES):
        raise SetupError("admin-api mint token answered other scopes")
    return user_id, token_id, token


async def watch_ws(ws_url: str, key: str, platform: str, native: str, watch: Watch,
                   done: asyncio.Event) -> None:
    """Subscribe to the meeting on /ws and feed every frame to `watch`; reconnect if it drops."""
    subscribe = json.dumps(
        {"action": "subscribe", "meetings": [{"platform": platform, "native_id": native}]}
    )
    while not done.is_set():
        try:
            async with ws_connect(
                ws_url, additional_headers={"X-API-Key": key}, proxy=None, open_timeout=15
            ) as ws:
                acked = False
                await ws.send(subscribe)
                sent = asyncio.get_running_loop().time()
                while not done.is_set():
                    if not acked and asyncio.get_running_loop().time() - sent >= SUBSCRIBE_RETRY_SECONDS:
                        await ws.send(subscribe)
                        sent = asyncio.get_running_loop().time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    arrived = utc_now()
                    try:
                        frame = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(frame, dict):
                        acked = acked or frame.get("type") == "subscribed"
                        watch.on_frame(frame, arrived)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the poll keeps watching; report and retry
            watch.emitter.emit("ws_dropped", error=type(exc).__name__)
        if not done.is_set():
            await asyncio.sleep(3)


async def poll_meeting(gateway: httpx.AsyncClient, meeting_id: int, watch: Watch,
                       done: asyncio.Event) -> None:
    while not done.is_set():
        try:
            asked = utc_now()
            response = await gateway.get(f"/meetings/{meeting_id}")
            if response.status_code == 200 and isinstance(response.json(), dict):
                watch.on_meeting(response.json(), utc_now(), asked=asked)
            else:
                watch.emitter.emit("poll_error", http=response.status_code)
        except httpx.HTTPError as exc:
            watch.emitter.emit("poll_error", error=type(exc).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=POLL_SECONDS)


async def wait_until(watch: Watch, stop: asyncio.Event, seconds: float | None) -> str:
    """Block until a terminal status, a stop request or `seconds`; returns which happened."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds if seconds else None
    while not watch.terminal:
        if stop.is_set():
            return "stop_requested"
        if deadline is not None and loop.time() >= deadline:
            return "stop_after"
        watch.changed.clear()
        waiters = [asyncio.ensure_future(watch.changed.wait()), asyncio.ensure_future(stop.wait())]
        timeout = None if deadline is None else max(0.0, deadline - loop.time())
        _, pending = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    return "terminal"


async def run(args: argparse.Namespace, emitter: Emitter) -> int:
    env_path = Path(args.env_file).expanduser()
    if not env_path.is_file():
        raise SetupError(f"env file not found: {env_path}")
    env = read_env(env_path)
    redactor = emitter.redactor
    admin_token = env.get("ADMIN_TOKEN", "")
    redactor.add(admin_token, "<admin-token>")
    stt_url = env.get("TRANSCRIPTION_SERVICE_URL", "")
    stt = ollm_tag(stt_url)
    redactor.add_url(stt_url, stt)
    # The meeting host (meet.google.com, …) stays readable; the code and query do not.
    redactor.add_url(args.url, redact_url(args.url), host=False)
    if not admin_token:
        raise SetupError("ADMIN_TOKEN is empty in the env file; run `make -C andai up` first")
    if "__OLLM_" in stt_url or "__OLLM_" in env.get("TRANSCRIPTION_MODEL", ""):
        raise SetupError("the env file still has __OLLM_*__ placeholders (andai/docs/live-test.md)")

    admin_url = f"http://{HOST}:{env.get('ADMIN_API_PORT') or 18057}"
    gateway_url = f"http://{HOST}:{env.get('API_GATEWAY_HOST_PORT') or 18056}"
    emitter.emit(
        "start",
        principal=PRINCIPAL_EMAIL,
        meeting=redact_url(args.url),
        language=args.language,
        stt_route=stt,
        stt_model=env.get("TRANSCRIPTION_MODEL") or None,
        stop_after_s=args.stop_after or None,
        rule="D-21: test meetings only, synthetic or consenting speech",
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()

    def on_sigint() -> None:
        if stop.is_set() and current is not None:
            current.cancel()  # a second Ctrl-C abandons the graceful wait
        stop.set()

    loop.add_signal_handler(signal.SIGINT, on_sigint)
    loop.add_signal_handler(signal.SIGTERM, on_sigint)

    async with httpx.AsyncClient(
        base_url=admin_url, headers={"X-Admin-API-Key": admin_token}, timeout=15, trust_env=False
    ) as admin:
        try:
            _, token_id, key = await ensure_principal(admin)
        except httpx.HTTPError as exc:
            raise SetupError(f"admin-api unreachable at {admin_url}: {type(exc).__name__}") from None
        redactor.add(key, "<api-key>")
        emitter.emit("principal_ready", scopes=list(SCOPES), capture_signal=False)
        try:
            return await dispatch_and_watch(args, emitter, gateway_url, key, stop)
        finally:
            with contextlib.suppress(httpx.HTTPError):
                revoked = await admin.delete(f"/admin/tokens/{token_id}")
                emitter.emit("key_revoked", http=revoked.status_code)


async def dispatch_and_watch(args: argparse.Namespace, emitter: Emitter, gateway_url: str,
                             key: str, stop: asyncio.Event) -> int:
    async with httpx.AsyncClient(
        base_url=gateway_url, headers={"X-API-Key": key}, timeout=30, trust_env=False
    ) as gateway:
        body: dict[str, object] = {
            "meeting_url": args.url,
            "bot_name": args.bot_name,
            "transcribe_enabled": True,
        }
        if args.language:
            body["language"] = args.language
        if args.disclosure:
            body["disclosure"] = {"text": args.disclosure, "privacy_notice_url": args.privacy_url}
        try:
            response = await gateway.post("/bots", json=body)
        except httpx.HTTPError as exc:
            raise SetupError(f"gateway unreachable at {gateway_url}: {type(exc).__name__}") from None
        if response.status_code not in (200, 201):
            raise SetupError(f"POST /bots answered {response.status_code}: {_detail(response)}")
        meeting = response.json()
        meeting_id = int(meeting["id"])
        platform = str(meeting.get("platform") or "")
        native = str(meeting.get("native_meeting_id") or "")
        emitter.redactor.add(native, redact_code(native))
        dispatched = utc_now()
        emitter.emit("dispatched", dispatched, meeting_id=meeting_id, platform=platform,
                     meeting_code=redact_code(native), http=response.status_code)

        watch = Watch(emitter)
        watch.on_meeting(meeting, dispatched, source="dispatch")
        done = asyncio.Event()
        ws_url = gateway_url.replace("http://", "ws://", 1) + "/ws"
        tasks = [
            asyncio.create_task(watch_ws(ws_url, key, platform, native, watch, done)),
            asyncio.create_task(poll_meeting(gateway, meeting_id, watch, done)),
        ]
        stopped = False
        try:
            reason = await wait_until(watch, stop, args.stop_after)
            if reason != "terminal":
                stopped = await stop_bot(gateway, platform, native, emitter, reason)
                await wait_until(watch, asyncio.Event(), STOP_GRACE_SECONDS)
        except asyncio.CancelledError:
            emitter.emit("abandoned", note="second interrupt; not waiting for the meeting to end")
        finally:
            if not watch.terminal and not stopped:
                with contextlib.suppress(Exception):
                    await stop_bot(gateway, platform, native, emitter, "harness_exit")
            done.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):  # the final row carries the end reason
                asked = utc_now()
                final = await gateway.get(f"/meetings/{meeting_id}")
                if final.status_code == 200:
                    watch.on_meeting(final.json(), utc_now(), asked=asked)
            emitter.emit("summary", meeting_id=meeting_id, **watch.summary())
        return 0 if watch.reached_active else 1


async def stop_bot(gateway: httpx.AsyncClient, platform: str, native: str, emitter: Emitter,
                   reason: str) -> bool:
    response = await gateway.delete(f"/bots/{platform}/{native}")
    emitter.emit("stop_sent", reason=reason, http=response.status_code)
    return response.status_code < 400


# ── entry point ──────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="live_dispatch.py",
        description=(
            "Dispatch the real bot into a live TEST meeting through the local gateway and print its "
            "status changes and transcript lag as redacted JSON lines (andai/docs/live-test.md). "
            "D-21: test meetings only, with synthetic or consenting speech."
        ),
    )
    parser.add_argument("--url", required=True, help="the meeting link (Meet, Teams or Zoom)")
    parser.add_argument("--language", default="en",
                        help="spoken-language code for transcription, e.g. en or hi (default en)")
    parser.add_argument("--env-file", default=os.environ.get("ENV_FILE") or "deploy/compose/.env",
                        help="the running stack's env file, as an absolute path (`uv run "
                             "--directory andai` runs from andai/; default: $ENV_FILE)")
    parser.add_argument("--stop-after", type=float, default=0, metavar="SECONDS",
                        help="stop the bot after this many seconds (default: run until Ctrl-C or "
                             "the meeting ends)")
    parser.add_argument("--bot-name", default=DEFAULT_BOT_NAME,
                        help=f"the bot's display name (default {DEFAULT_BOT_NAME!r})")
    parser.add_argument("--disclosure", nargs="?", const=DEFAULT_DISCLOSURE, metavar="TEXT",
                        help="ask the bot to post this recording notice in meeting chat once admitted "
                             "(no TEXT: the default notice); needs --privacy-url")
    parser.add_argument("--privacy-url", metavar="URL",
                        help="the privacy-notice link appended to the --disclosure message (http/https)")
    parser.add_argument("--log", help="also append the output lines to this file (absolute path)")
    args = parser.parse_args(argv)
    if args.disclosure and not args.privacy_url:
        parser.error("--disclosure needs --privacy-url")
    if args.privacy_url and not args.disclosure:
        parser.error("--privacy-url is only used with --disclosure")
    if args.privacy_url and urlsplit(args.privacy_url).scheme not in ("http", "https"):
        parser.error("--privacy-url must be an http:// or https:// URL")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log: TextIO | None = None
    if args.log:
        log_path = Path(args.log).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a", encoding="utf-8")
    emitter = Emitter(Redactor(), log=log)
    try:
        return asyncio.run(run(args, emitter))
    except SetupError as exc:
        emitter.emit("setup_error", error=str(exc))
        return 2
    except (KeyboardInterrupt, asyncio.CancelledError):
        emitter.emit("abandoned", note="interrupted")
        return 1
    finally:
        if log is not None:
            log.close()


if __name__ == "__main__":
    sys.exit(main())
