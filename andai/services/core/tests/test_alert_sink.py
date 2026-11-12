"""The alert sink catches Slack and Teams posts, and its scan finds every identifying pattern.

The sink records each body to its file and answers 200 `ok`; `/captured` gives counts only. The
scan counts each pattern class (email, UUID, URL, IPv4, IPv6, Meet code, Zoom id, Teams link) in
planted bodies, prints counts and never the matched text, exits 1 on any hit, and 0 on the
committed clean fixture (Slack and Teams bodies as Alertmanager renders them from `munshi.tmpl`).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from andai_core.testing import alert_sink

FIXTURE = Path(__file__).parent / "fixtures" / "alert_sink_clean.jsonl"

PLANTED = {
    "email": "owner was someone@example.com",
    "uuid": "meeting 3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c failed",
    "url": "see https://portal.example.com/meetings/1",
    "ipv4": "from 203.0.113.42",
    "ipv6": "from 2001:db8::8a2e:370:7334",
    "meet_code": "meet code abc-defg-hij",
    "zoom_id": "zoom 84512345678",
    "teams_link": "join at teams.example.com/l/meetup-join/19%3ameeting",
}


@pytest.fixture
def sink(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    out = tmp_path / "captured.jsonl"
    server = alert_sink.make_server("127.0.0.1", 0, out)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", out
    finally:
        server.shutdown()
        server.server_close()


def post(url: str, body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def scan_cli(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "andai_core.testing.alert_sink", "--scan", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_sink_records_slack_and_teams_posts_and_answers_ok(sink: tuple[str, Path]) -> None:
    base, out = sink
    slack = {"username": "munshi.ai", "attachments": [{"title": "[FIRING] X — pipeline"}]}
    teams = {"type": "message", "attachments": []}

    assert post(f"{base}/slack", json.dumps(slack).encode()) == (200, b"ok")
    assert post(f"{base}/teams", json.dumps(teams).encode()) == (200, b"ok")
    assert post(f"{base}/teams", b"not json") == (200, b"ok")

    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines == [
        {"receiver": "slack", "body": slack},
        {"receiver": "teams", "body": teams},
        {"receiver": "teams", "body": "not json"},
    ]
    status, counts = get(f"{base}/captured")
    assert status == 200
    assert json.loads(counts) == {"slack": 1, "teams": 2}
    assert get(f"{base}/healthz") == (200, b"ok")


def test_the_sink_refuses_other_paths(sink: tuple[str, Path]) -> None:
    base, out = sink
    assert post(f"{base}/forward", b"{}")[0] == 404
    assert get(f"{base}/captured/1")[0] == 404
    assert not out.exists()


@pytest.mark.parametrize("pattern", sorted(PLANTED))
def test_the_scan_finds_each_pattern_class_and_prints_counts_only(
    tmp_path: Path, pattern: str
) -> None:
    captured = tmp_path / "captured.jsonl"
    body = {"attachments": [{"title": "[FIRING] MunshiCanaryFailing — bot-joins",
                             "text": PLANTED[pattern]}]}
    captured.write_text(json.dumps({"receiver": "slack", "body": body}) + "\n")

    result = scan_cli(captured)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["records"] == 1
    assert report["hits"][pattern] >= 1, report
    assert PLANTED[pattern] not in result.stdout + result.stderr
    for text in PLANTED.values():
        for word in text.split():
            if any(ch.isdigit() for ch in word) or "@" in word or "/" in word:
                assert word not in result.stdout


def test_the_scan_counts_every_class_over_many_bodies(tmp_path: Path) -> None:
    captured = tmp_path / "captured.jsonl"
    captured.write_text(
        "".join(json.dumps({"receiver": "teams", "body": text}) + "\n" for text in PLANTED.values())
    )
    report = alert_sink.scan_file(captured)
    assert report["records"] == len(PLANTED)
    assert all(report["hits"][name] >= 1 for name in PLANTED), report


def test_only_the_exact_card_schema_constant_is_ignored() -> None:
    hits, ignored = alert_sink.scan_text(
        '{"$schema": "http://adaptivecards.io/schemas/adaptive-card.json"}'
    )
    assert ignored == 1 and sum(hits.values()) == 0
    hits, ignored = alert_sink.scan_text('{"$schema": "http://adaptivecards.io/schemas/other.json"}')
    assert ignored == 0 and hits["url"] == 1


def test_the_committed_clean_fixture_scans_clean() -> None:
    result = scan_cli(FIXTURE)
    assert result.returncode == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["records"] == 6
    assert set(report["hits"].values()) == {0}
    receivers = {json.loads(line)["receiver"] for line in FIXTURE.read_text().splitlines()}
    assert receivers == {"slack", "teams"}


def test_the_clean_fixture_is_what_the_munshi_templates_render() -> None:
    """Every title in the fixture has the munshi.title shape and every text the munshi.text shape."""
    title = r"^\[(FIRING|RESOLVED)\] Munshi[A-Za-z]+ — [a-z-]+$"
    text = r"^\d+ firing, \d+ resolved\n(- (critical|warning): [^\n]+ \((firing|resolved)\)\n)+$"
    for line in FIXTURE.read_text().splitlines():
        record = json.loads(line)
        body = record["body"]
        if record["receiver"] == "slack":
            attachment = body["attachments"][0]
            assert "title_link" not in attachment
            pairs = [(attachment["title"], attachment["text"])]
            assert attachment["fallback"] == attachment["title"]
        else:
            blocks = body["attachments"][0]["content"]["body"]
            pairs = [(blocks[0]["text"], blocks[1]["text"])]
        for got_title, got_text in pairs:
            assert re.match(title, got_title), got_title
            assert re.match(text, got_text), got_text
