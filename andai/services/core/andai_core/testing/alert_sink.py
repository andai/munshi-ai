"""A local stand-in for Slack and Microsoft Teams webhooks, and a PII scan of what it caught.

Test and e2e use only (the Compose `monitoring-test` profile runs it as `alert-sink`); no default
stack starts it, and it never forwards anything. Alertmanager's Slack and Teams receivers post to it
when the operator's URL files point at `http://alert-sink:8000/slack` and `.../teams`:

* `POST /slack`, `POST /teams` — appends `{"receiver": "slack"|"teams", "body": <parsed JSON or
  the raw text>}` as one line to `--out` inside the container and answers 200 `ok` (the answer
  Slack's incoming webhooks give; Teams accepts any 2xx);
* `GET /captured` — how many bodies each path caught, as JSON (never the bodies);
* `GET /healthz` — for the compose health check.

`python -m andai_core.testing.alert_sink --scan FILE` reads captured lines (or any file of JSON
lines with a `body`) and counts what must never be in an alert: email addresses, UUIDs, URLs
(`http://`, `https://`), IPv4 and IPv6 addresses, Google Meet codes (`abc-defg-hij`), Zoom meeting
ids (9 to 11 digits) and Teams meeting paths (`/meet/`, `meetup-join`). It prints the counts per
pattern only, never the matched text, and exits 1 on any hit, 0 when clean. The Adaptive Card
schema URL that Alertmanager's Teams receiver writes into every card is a protocol constant, not
alert text; it is removed before scanning and counted as `ignored_constants`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 1024 * 1024
RECEIVERS = {"/slack": "slack", "/teams": "teams"}
# Fixed by Alertmanager's msteamsv2 receiver (`$schema` of every Adaptive Card it posts).
IGNORED_CONSTANTS = ("http://adaptivecards.io/schemas/adaptive-card.json",)

_HEX = "[0-9A-Fa-f]"
_V6_GROUP = f"{_HEX}{{1,4}}"
PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    "uuid": re.compile(rf"(?<!{_HEX}){_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}(?!{_HEX})"),
    "url": re.compile(r"https?://", re.IGNORECASE),
    "ipv4": re.compile(
        r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\d.])"
    ),
    "ipv6": re.compile(
        rf"(?<![0-9A-Fa-f:])(?:"
        rf"(?:{_V6_GROUP}:){{7}}{_V6_GROUP}"
        rf"|(?:{_V6_GROUP}:){{1,7}}:(?:{_V6_GROUP}(?::{_V6_GROUP}){{0,6}})?"
        rf"|::{_V6_GROUP}(?::{_V6_GROUP}){{0,6}}"
        rf")(?![0-9A-Fa-f:])"
    ),
    "meet_code": re.compile(r"(?<![A-Za-z-])[a-z]{3}-[a-z]{4}-[a-z]{3}(?![A-Za-z-])"),
    "zoom_id": re.compile(r"(?<!\d)\d{9,11}(?!\d)"),
    "teams_link": re.compile(r"/meet/|meetup-join", re.IGNORECASE),
}


def scan_text(text: str) -> tuple[Counter[str], int]:
    """Hits per pattern in `text`, and how many ignored protocol constants were removed."""
    ignored = 0
    for constant in IGNORED_CONSTANTS:
        ignored += text.count(constant)
        text = text.replace(constant, " ")
    hits: Counter[str] = Counter({name: 0 for name in PATTERNS})
    for name, pattern in PATTERNS.items():
        hits[name] += len(pattern.findall(text))
    return hits, ignored


def _body_text(line: str) -> str:
    record = json.loads(line)
    body = record.get("body", "") if isinstance(record, dict) else record
    return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)


def scan_file(path: Path) -> dict:
    """Counts over every captured body in `path` (one JSON object per line)."""
    total: Counter[str] = Counter({name: 0 for name in PATTERNS})
    records = ignored = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        hits, skipped = scan_text(_body_text(line))
        total.update(hits)
        ignored += skipped
        records += 1
    return {"records": records, "hits": dict(total), "ignored_constants": ignored}


class _Sink:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.lock = threading.Lock()
        self.counts: Counter[str] = Counter({name: 0 for name in RECEIVERS.values()})

    def record(self, receiver: str, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace")
        try:
            body: object = json.loads(text)
        except ValueError:
            body = text
        line = json.dumps({"receiver": receiver, "body": body}, ensure_ascii=False)
        with self.lock:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            with self.out.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self.counts[receiver] += 1


def make_handler(sink: _Sink) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "alert-sink"

        def _answer(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            receiver = RECEIVERS.get(self.path)
            if receiver is None:
                self._answer(404, b"not found")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY:
                self._answer(413, b"too large")
                return
            sink.record(receiver, self.rfile.read(length))
            self._answer(200, b"ok")

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/healthz":
                self._answer(200, b"ok")
            elif self.path == "/captured":
                with sink.lock:
                    counts = dict(sink.counts)
                self._answer(200, json.dumps(counts).encode(), "application/json")
            else:
                self._answer(404, b"not found")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return  # bodies and peers stay out of the logs

    return Handler


def make_server(host: str, port: int, out: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(_Sink(out)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m andai_core.testing.alert_sink")
    parser.add_argument("--scan", type=Path, help="count PII patterns in a captured file and exit")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--out", type=Path, default=Path("/tmp/alert-sink/captured.jsonl"))
    args = parser.parse_args(argv)
    if args.scan is not None:
        result = scan_file(args.scan)
        print(json.dumps(result, sort_keys=True))
        return 1 if any(result["hits"].values()) else 0
    server = make_server(args.host, args.port, args.out)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
