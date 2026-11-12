"""Alertmanager's config, rendered from the operator's settings (OPS-05, D-62, D-63).

    python -m andai_worker.alerting_config --out /etc/alertmanager/munshi/alertmanager.yml \\
        --secrets-dir /etc/alertmanager/secrets [--templates-dir /etc/alertmanager/templates] \\
        [--state-file /run/munshi-alerting/state.json] [--owner 65534:65534]

The Compose `monitoring` profile runs this once, from the andai-worker image, before Alertmanager
starts (`alertmanager-config`).

Every receiver is off by default. The route sends every alert, grouped by alert name and service,
to one receiver: `none` (no integrations) or `munshi`, which holds each integration the operator
turned on:

* **email** when `ANDAI_ALERT_EMAIL` is set, through the stack's relay (`SMTP_HOST`, `SMTP_PORT`,
  `SMTP_FROM`, `SMTP_USER`, `SMTP_STARTTLS`). `SMTP_PASSWORD` is written to `smtp_password` next to
  the config (mode 0600, owned by `--owner`) and referenced as `auth_password_file`;
* **Slack** when `ANDAI_ALERT_SLACK=1` and `<secrets-dir>/slack_webhook_url` exists
  (`api_url_file`);
* **Microsoft Teams** when `ANDAI_ALERT_TEAMS=1` and `<secrets-dir>/teams_webhook_url` exists, as
  a Workflows webhook (`msteamsv2_configs`, `webhook_url_file`; the retired Office 365 connector
  receiver is never written).

Webhook URLs are credentials: they stay in the operator's files, which this renderer only checks
for existence and never reads, so no URL is ever in the rendered config, an env value or a log.
Every text field points at the `munshi.title` / `munshi.text` templates
(`andai/deploy/monitoring/alertmanager/templates/munshi.tmpl`), which print only the alert name,
service, severity and the rule's fixed summary. Slack's title link is empty and email has no HTML
part, so Alertmanager's default templates (every label, annotation and a link back) never run.

The config is written as JSON, which is YAML. The renderer also writes the receivers state file
(`ANDAI_ALERTING_STATE_FILE`): `{"receivers": N, "names": [...]}`. andai-worker reads it
(`AlertMailGate` in `alerts.py`): with `ANDAI_MONITORING` on and at least one receiver, Alertmanager
owns alerts and the worker's own alert mail stands down; otherwise the worker keeps mailing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from andai_worker.settings import WorkerSettings

log = logging.getLogger("andai_worker.alerting_config")

RECEIVER_NONE = "none"
RECEIVER = "munshi"
RECEIVER_NAMES = ("email", "slack", "teams")
SLACK_URL_FILE = "slack_webhook_url"
TEAMS_URL_FILE = "teams_webhook_url"
SMTP_PASSWORD_FILE = "smtp_password"
DEFAULT_SECRETS_DIR = Path("/etc/alertmanager/secrets")
DEFAULT_TEMPLATES_DIR = Path("/etc/alertmanager/templates")
TITLE = '{{ template "munshi.title" . }}'
TEXT = '{{ template "munshi.text" . }}'
ROUTE = {
    "group_by": ["alertname", "service"],
    "group_wait": "30s",
    "group_interval": "5m",
    "repeat_interval": "4h",
}
HEADER = (
    "# Rendered by `python -m andai_worker.alerting_config` from the operator's settings; do not\n"
    "# edit. JSON is YAML. andai/docs/monitoring.md explains each receiver.\n"
)


@dataclass(frozen=True)
class AlertingState:
    """Which receivers the rendered config enabled, for andai-worker."""

    receivers: int
    names: tuple[str, ...]

    def as_json(self) -> dict:
        return {"receivers": self.receivers, "names": list(self.names)}


@dataclass(frozen=True)
class Rendered:
    config: dict
    state: AlertingState
    smtp_password: str | None


def _present(path: Path) -> bool:
    """The operator's URL file exists and is not empty; its content is never read."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def render(
    settings: WorkerSettings,
    *,
    out_dir: Path,
    secrets_dir: Path = DEFAULT_SECRETS_DIR,
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
    present: Callable[[Path], bool] = _present,
) -> Rendered:
    """The Alertmanager config for `settings`; pure apart from the existence checks."""
    integrations: dict[str, list[dict]] = {}
    names: list[str] = []
    password: str | None = None

    if settings.alert_to:
        email: dict = {
            "to": settings.alert_to,
            "from": settings.smtp_from,
            "smarthost": f"{settings.smtp_host}:{settings.smtp_port}",
            "require_tls": settings.smtp_starttls,
            "send_resolved": True,
            "headers": {"Subject": TITLE},
            "html": "",
            "text": TEXT,
        }
        if settings.smtp_user:
            email["auth_username"] = settings.smtp_user
        if settings.smtp_password is not None and settings.smtp_password.get_secret_value():
            password = settings.smtp_password.get_secret_value()
            email["auth_password_file"] = str(out_dir / SMTP_PASSWORD_FILE)
        integrations["email_configs"] = [email]
        names.append("email")

    slack_file = secrets_dir / SLACK_URL_FILE
    if settings.alert_slack:
        if present(slack_file):
            integrations["slack_configs"] = [
                {
                    "api_url_file": str(slack_file),
                    "send_resolved": True,
                    "username": "munshi.ai",
                    "title": TITLE,
                    "title_link": "",
                    "text": TEXT,
                    "fallback": TITLE,
                    "pretext": "",
                    "footer": "",
                    "callback_id": "",
                    "icon_emoji": "",
                    "icon_url": "",
                }
            ]
            names.append("slack")
        else:
            log.warning("ANDAI_ALERT_SLACK is on but %s is missing or empty: Slack stays off",
                        slack_file)

    teams_file = secrets_dir / TEAMS_URL_FILE
    if settings.alert_teams:
        if present(teams_file):
            integrations["msteamsv2_configs"] = [
                {
                    "webhook_url_file": str(teams_file),
                    "send_resolved": True,
                    "title": TITLE,
                    "text": TEXT,
                }
            ]
            names.append("teams")
        else:
            log.warning("ANDAI_ALERT_TEAMS is on but %s is missing or empty: Teams stays off",
                        teams_file)

    receiver = {"name": RECEIVER if names else RECEIVER_NONE, **integrations}
    config = {
        "route": {"receiver": receiver["name"], **ROUTE},
        "receivers": [receiver],
        "templates": [str(templates_dir / "*.tmpl")],
    }
    return Rendered(config, AlertingState(len(names), tuple(names)), password)


def _write(path: Path, content: str, mode: int, owner: tuple[int, int] | None) -> None:
    """Atomically replace `path`; mode and owner are set before the rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp, mode)
        if owner is not None:
            os.chown(tmp, *owner)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write(
    rendered: Rendered,
    *,
    out: Path,
    state_file: Path,
    owner: tuple[int, int] | None = None,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    password_file = out.parent / SMTP_PASSWORD_FILE
    if rendered.smtp_password is not None:
        _write(password_file, rendered.smtp_password, 0o600, owner)
    else:
        password_file.unlink(missing_ok=True)
    _write(out, HEADER + json.dumps(rendered.config, indent=2) + "\n", 0o644, owner)
    _write(state_file, json.dumps(rendered.state.as_json()) + "\n", 0o644, None)


def read_state(path: Path) -> AlertingState | None:
    """The receivers state file, or None when it is missing, unreadable or malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    receivers, names = data.get("receivers"), data.get("names")
    if (
        not isinstance(receivers, int)
        or isinstance(receivers, bool)
        or receivers < 0
        or not isinstance(names, list)
        or not all(name in RECEIVER_NAMES for name in names)
        or len(names) != receivers
    ):
        return None
    return AlertingState(receivers, tuple(names))


def _owner(value: str) -> tuple[int, int]:
    uid, _, gid = value.partition(":")
    return int(uid), int(gid or uid)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m andai_worker.alerting_config")
    parser.add_argument("--out", type=Path, required=True, help="alertmanager.yml to write")
    parser.add_argument("--secrets-dir", type=Path, default=DEFAULT_SECRETS_DIR)
    parser.add_argument("--templates-dir", type=Path, default=DEFAULT_TEMPLATES_DIR)
    parser.add_argument("--state-file", type=Path, default=None,
                        help="default: ANDAI_ALERTING_STATE_FILE")
    parser.add_argument("--owner", type=_owner, default=None,
                        help="UID:GID that owns the config and password file (Alertmanager's user)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = WorkerSettings()
    rendered = render(
        settings,
        out_dir=args.out.parent,
        secrets_dir=args.secrets_dir,
        templates_dir=args.templates_dir,
    )
    state_file = args.state_file or settings.alerting_state_file
    write(rendered, out=args.out, state_file=state_file, owner=args.owner)
    names = ", ".join(rendered.state.names) or "none"
    log.info("alertmanager config written: %d receiver(s): %s", rendered.state.receivers, names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
