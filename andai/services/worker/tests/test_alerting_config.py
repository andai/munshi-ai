"""Alertmanager's rendered config: every receiver off by default, webhooks only from files, text
only from the munshi templates; and the worker's alert mail stands down only when Alertmanager has
a receiver (OPS-05, D-62, D-63).

The renderer and template checks need no Docker; the last tests run the worker's alert evaluator
against its own database.
"""

from __future__ import annotations

import json
import logging
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine, select

from andai_core.db.models import AlertState
from andai_core.db.session import transaction
from andai_core.mail import MemoryMailer
from andai_worker import alerting_config
from andai_worker.alerting_config import AlertingState, read_state, render, write
from andai_worker.alerts import AlertEvaluator, AlertMailGate
from andai_worker.settings import DEFAULT_ALERTING_STATE_FILE, WorkerSettings

from .test_worker_alerts import OPERATOR, canary, tick
from .worker_support import Clock

TEMPLATE = (
    Path(__file__).resolve().parents[3]
    / "deploy"
    / "monitoring"
    / "alertmanager"
    / "templates"
    / "munshi.tmpl"
)
TITLE = '{{ template "munshi.title" . }}'
TEXT = '{{ template "munshi.text" . }}'
SINK_URL = "http://alert-sink:8000/slack"
INTEGRATIONS = {"email_configs", "slack_configs", "msteamsv2_configs"}


def settings(**env: str) -> WorkerSettings:
    base = {
        "ANDAI_ENV": "dev",
        "ANDAI_ALERT_EMAIL": "",
        "ANDAI_ALERT_SLACK": "0",
        "ANDAI_ALERT_TEAMS": "0",
        "SMTP_HOST": "mailpit",
        "SMTP_PORT": "1025",
        "SMTP_USER": "",
        "SMTP_PASSWORD": "",
        "SMTP_FROM": "munshi.ai <no-reply@munshi.invalid>",
        "SMTP_STARTTLS": "false",
    }
    base.update(env)
    return WorkerSettings(**base)


def secrets(tmp_path: Path, *names: str) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir(exist_ok=True)
    for name in names:
        # The renderer must never read these; they hold what a real webhook URL looks like.
        (directory / name).write_text(SINK_URL + "\n")
    return directory


def rendered_text(tmp_path: Path, config: WorkerSettings, secrets_dir: Path) -> tuple[dict, str]:
    out = tmp_path / "out" / "alertmanager.yml"
    result = render(config, out_dir=out.parent, secrets_dir=secrets_dir)
    write(result, out=out, state_file=tmp_path / "state.json")
    text = out.read_text()
    return yaml.safe_load(text), text


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def receiver(config: dict) -> dict:
    assert len(config["receivers"]) == 1
    only = config["receivers"][0]
    assert config["route"]["receiver"] == only["name"]
    return only


# ── renderer ─────────────────────────────────────────────────────────────────────────────────────


def test_by_default_everything_routes_to_a_receiver_with_no_integrations(tmp_path: Path) -> None:
    config, text = rendered_text(tmp_path, settings(), secrets(tmp_path))

    assert receiver(config) == {"name": "none"}
    assert config["route"] == {
        "receiver": "none",
        "group_by": ["alertname", "service"],
        "group_wait": "30s",
        "group_interval": "5m",
        "repeat_interval": "4h",
    }
    assert config["templates"] == ["/etc/alertmanager/templates/*.tmpl"]
    assert json.loads((tmp_path / "state.json").read_text()) == {"receivers": 0, "names": []}
    assert not (tmp_path / "out" / "smtp_password").exists()
    assert "http://" not in text and "https://" not in text


def test_the_alert_address_enables_email_through_the_stack_relay(tmp_path: Path) -> None:
    config, text = rendered_text(
        tmp_path,
        settings(
            ANDAI_ALERT_EMAIL="ops@example.test",
            SMTP_HOST="relay.example.test",
            SMTP_PORT="587",
            SMTP_USER="alerts",
            SMTP_PASSWORD="relay-pass-123",
            SMTP_STARTTLS="true",
        ),
        secrets(tmp_path),
    )

    only = receiver(config)
    assert only["name"] == "munshi"
    assert set(only) == {"name", "email_configs"}
    email = only["email_configs"][0]
    assert email == {
        "to": "ops@example.test",
        "from": "munshi.ai <no-reply@munshi.invalid>",
        "smarthost": "relay.example.test:587",
        "require_tls": True,
        "send_resolved": True,
        "headers": {"Subject": TITLE},
        "html": "",
        "text": TEXT,
        "auth_username": "alerts",
        "auth_password_file": str(tmp_path / "out" / "smtp_password"),
    }
    password = tmp_path / "out" / "smtp_password"
    assert password.read_text() == "relay-pass-123"
    assert stat.S_IMODE(password.stat().st_mode) == 0o600
    assert "relay-pass-123" not in text
    assert json.loads((tmp_path / "state.json").read_text()) == {
        "receivers": 1,
        "names": ["email"],
    }


def test_email_without_a_relay_password_has_no_password_file(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "smtp_password").write_text("stale")
    config, _ = rendered_text(
        tmp_path, settings(ANDAI_ALERT_EMAIL="ops@example.test"), secrets(tmp_path)
    )

    email = receiver(config)["email_configs"][0]
    assert "auth_password_file" not in email and "auth_username" not in email
    assert email["require_tls"] is False
    assert not (tmp_path / "out" / "smtp_password").exists()


@pytest.mark.parametrize(
    ("flags", "files", "names"),
    [
        ({"ANDAI_ALERT_SLACK": "1"}, (), []),
        ({}, ("slack_webhook_url",), []),
        ({"ANDAI_ALERT_SLACK": "1"}, ("slack_webhook_url",), ["slack"]),
        ({"ANDAI_ALERT_TEAMS": "1"}, (), []),
        ({}, ("teams_webhook_url",), []),
        ({"ANDAI_ALERT_TEAMS": "1"}, ("teams_webhook_url",), ["teams"]),
        (
            {"ANDAI_ALERT_SLACK": "1", "ANDAI_ALERT_TEAMS": "1"},
            ("slack_webhook_url", "teams_webhook_url"),
            ["slack", "teams"],
        ),
    ],
)
def test_webhooks_need_both_the_flag_and_the_url_file(
    tmp_path: Path, flags: dict, files: tuple, names: list, caplog: pytest.LogCaptureFixture
) -> None:
    directory = secrets(tmp_path, *files)
    with caplog.at_level(logging.WARNING, logger="andai_worker.alerting_config"):
        config, text = rendered_text(tmp_path, settings(**flags), directory)

    state = json.loads((tmp_path / "state.json").read_text())
    assert state == {"receivers": len(names), "names": names}
    only = receiver(config)
    assert only["name"] == ("munshi" if names else "none")
    assert ("slack_configs" in only) == ("slack" in names)
    assert ("msteamsv2_configs" in only) == ("teams" in names)
    if "slack" in names:
        assert only["slack_configs"][0]["api_url_file"] == str(directory / "slack_webhook_url")
    if "teams" in names:
        assert only["msteamsv2_configs"][0]["webhook_url_file"] == str(
            directory / "teams_webhook_url"
        )
    warned = [r for r in caplog.records if "stays off" in r.getMessage()]
    assert len(warned) == len(flags) - len(names)
    assert SINK_URL not in text
    assert "http://" not in text and "https://" not in text


def test_everything_on_renders_three_integrations_with_munshi_text_only(tmp_path: Path) -> None:
    directory = secrets(tmp_path, "slack_webhook_url", "teams_webhook_url")
    config, text = rendered_text(
        tmp_path,
        settings(
            ANDAI_ALERT_EMAIL="ops@example.test",
            ANDAI_ALERT_SLACK="1",
            ANDAI_ALERT_TEAMS="1",
            SMTP_PASSWORD="relay-pass-123",
        ),
        directory,
    )

    only = receiver(config)
    assert set(only) - {"name"} == INTEGRATIONS
    assert "msteams_configs" not in text
    assert not re.search(r"https?://", text)
    assert "relay-pass-123" not in text
    slack = only["slack_configs"][0]
    assert slack["title_link"] == ""
    assert slack["title"] == TITLE and slack["fallback"] == TITLE and slack["text"] == TEXT
    assert slack["pretext"] == "" and slack["footer"] == "" and slack["callback_id"] == ""
    teams = only["msteamsv2_configs"][0]
    assert teams["title"] == TITLE and teams["text"] == TEXT
    email = only["email_configs"][0]
    assert email["headers"] == {"Subject": TITLE} and email["html"] == "" and email["text"] == TEXT
    for integration in INTEGRATIONS:
        assert only[integration][0]["send_resolved"] is True
    # Every templated field names a munshi template and nothing else.
    for integration in INTEGRATIONS:
        for value in _strings(only[integration][0]):
            if "{{" in value:
                assert value in (TITLE, TEXT), value
    assert json.loads((tmp_path / "state.json").read_text()) == {
        "receivers": 3,
        "names": ["email", "slack", "teams"],
    }


def test_the_url_files_are_never_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = secrets(tmp_path, "slack_webhook_url", "teams_webhook_url")
    reads: list[str] = []
    real_read_text, real_open = Path.read_text, Path.open

    def read_text(self: Path, *args, **kwargs):  # noqa: ANN202
        reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    def path_open(self: Path, *args, **kwargs):  # noqa: ANN202
        reads.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "open", path_open)
    render(
        settings(ANDAI_ALERT_SLACK="1", ANDAI_ALERT_TEAMS="1"),
        out_dir=tmp_path,
        secrets_dir=directory,
    )
    assert not [p for p in reads if p.startswith(str(directory))]


def test_the_cli_writes_config_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for key, value in {
        "ANDAI_ENV": "dev",
        "ANDAI_ALERT_EMAIL": "ops@example.test",
        "ANDAI_ALERT_SLACK": "1",
        "ANDAI_ALERT_TEAMS": "0",
        "SMTP_PASSWORD": "relay-pass-123",
        "ANDAI_ALERTING_STATE_FILE": str(tmp_path / "run" / "state.json"),
    }.items():
        monkeypatch.setenv(key, value)
    directory = secrets(tmp_path, "slack_webhook_url")
    out = tmp_path / "config" / "alertmanager.yml"

    with caplog.at_level(logging.INFO, logger="andai_worker.alerting_config"):
        code = alerting_config.main(["--out", str(out), "--secrets-dir", str(directory)])

    assert code == 0
    assert out.read_text().startswith("# Rendered by")
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert read_state(tmp_path / "run" / "state.json") == AlertingState(2, ("email", "slack"))
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "2 receiver(s): email, slack" in logged
    assert "relay-pass-123" not in logged and SINK_URL not in logged


def test_a_blank_state_file_setting_is_the_default_path() -> None:
    assert settings(ANDAI_ALERTING_STATE_FILE="").alerting_state_file == DEFAULT_ALERTING_STATE_FILE


# ── the template ─────────────────────────────────────────────────────────────────────────────────

ALLOWED_FIELDS = {
    ".Status",
    ".Alerts",
    ".Alerts.Firing",
    ".Alerts.Resolved",
    ".Labels.alertname",
    ".Labels.service",
    ".Labels.severity",
    ".Annotations.summary",
}


def test_the_template_references_only_name_service_severity_summary_status_and_counts() -> None:
    source = TEMPLATE.read_text()
    without_comments = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", source, flags=re.DOTALL)
    actions = re.findall(r"\{\{(.*?)\}\}", without_comments, flags=re.DOTALL)
    fields = set()
    for action in actions:
        action = re.sub(r'"[^"]*"', "", action)  # string literals (template names, "firing")
        fields.update(re.findall(r"(?<![\w)])\.[A-Za-z_][A-Za-z0-9_.]*", action))
    assert fields <= ALLOWED_FIELDS, sorted(fields - ALLOWED_FIELDS)
    assert {".Labels.alertname", ".Labels.service", ".Annotations.summary"} <= fields
    defined = set(re.findall(r'define "([^"]+)"', without_comments))
    assert defined == {"munshi.title", "munshi.text"}
    for forbidden in ("CommonLabels", "CommonAnnotations", "GroupLabels", "ExternalURL",
                      "GeneratorURL", "SortedPairs", "Receiver", "template \""):
        assert forbidden not in without_comments


# ── the state file and the worker's gate ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '{"receivers": "1", "names": ["email"]}',
     '{"receivers": 1, "names": ["pagerduty"]}', '{"receivers": 2, "names": ["email"]}',
     '{"receivers": true, "names": ["email"]}', '{"receivers": -1, "names": []}'],
)
def test_a_malformed_state_file_reads_as_none(tmp_path: Path, content: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(content)
    assert read_state(path) is None


def test_a_missing_state_file_reads_as_none(tmp_path: Path) -> None:
    assert read_state(tmp_path / "absent.json") is None


def write_state(path: Path, *names: str) -> None:
    path.write_text(json.dumps({"receivers": len(names), "names": list(names)}))


def test_without_monitoring_the_worker_mail_is_always_on(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    write_state(state, "email")
    assert AlertMailGate(False, state)() is True


@pytest.mark.parametrize("names", [None, ()])
def test_monitoring_with_no_receiver_keeps_the_worker_mail_and_warns_once(
    tmp_path: Path, names: tuple | None, caplog: pytest.LogCaptureFixture
) -> None:
    state = tmp_path / "state.json"
    if names is not None:
        write_state(state, *names)
    gate = AlertMailGate(True, state)
    with caplog.at_level(logging.INFO, logger="andai_worker.alerts"):
        assert gate() is True
        assert gate() is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "monitoring on but no alert receiver enabled; worker alert mail stays on"
    ]


def test_monitoring_with_a_receiver_stands_the_worker_mail_down(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = tmp_path / "state.json"
    write_state(state, "slack", "teams")
    gate = AlertMailGate(True, state)
    with caplog.at_level(logging.INFO, logger="andai_worker.alerts"):
        assert gate() is False
        assert gate() is False
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "Alertmanager owns alerts" in messages[0] and "slack, teams" in messages[0]


def _two_canary_failures(engine: Engine) -> None:
    t = datetime.now(UTC) - timedelta(hours=2)
    canary(engine, "failure", reason="admission_denied", at=t)
    canary(engine, "failure", reason="admission_denied", at=t + timedelta(hours=1))


def _incidents(engine: Engine) -> int:
    with transaction(engine) as tx:
        return len(tx.scalars(select(AlertState)).all())


def test_the_evaluator_mails_when_monitoring_has_no_receiver(
    fresh_app_engine: Engine, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    write_state(state)
    mailer = MemoryMailer()
    ev = AlertEvaluator(fresh_app_engine, mailer, OPERATOR, clock=Clock(),
                        gate=AlertMailGate(True, state))
    _two_canary_failures(fresh_app_engine)

    assert tick(ev) == 1
    assert [m.to for m in mailer.outbox] == [OPERATOR]


def test_the_evaluator_stands_down_while_alertmanager_has_a_receiver_and_resumes_without_one(
    fresh_app_engine: Engine, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    write_state(state, "email")
    mailer = MemoryMailer()
    ev = AlertEvaluator(fresh_app_engine, mailer, OPERATOR, clock=Clock(),
                        gate=AlertMailGate(True, state))
    _two_canary_failures(fresh_app_engine)

    assert tick(ev) == 0
    assert mailer.outbox == []
    assert _incidents(fresh_app_engine) == 0

    write_state(state)  # the operator re-rendered with no receiver
    assert tick(ev) == 1
    assert len(mailer.outbox) == 1
