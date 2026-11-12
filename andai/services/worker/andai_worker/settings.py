"""andai-worker settings: andai-core's settings class plus the canary, alert and scheduled-dispatch
settings.

The worker reads the andai database as `andai_app` (`ANDAI_DB_URL_APP`), unseals the canary org's
platform key with the same KEK (`ANDAI_KEK`), dispatches through the platform gateway and mails
through the same SMTP relay as andai-core, so it shares `andai_core.settings.Settings`.

Canary links are standing test meetings that admit guests without a human (one per platform; an
empty value turns that platform's canary off). A Zoom link may carry its passcode, so the links are
`SecretStr` and never logged.

Meetings scheduled from invites are dispatched `ANDAI_INTAKE_JOIN_LEAD_S` seconds before they start
(default 120, D-58), checked every `ANDAI_SCHEDULED_TICK_S` seconds (default 15, at most 30).

Monitoring (OPS-05, D-63): `ANDAI_MONITORING` says the Prometheus/Alertmanager stack runs. The
worker's own alert mail then stands down only while the receivers state file
(`ANDAI_ALERTING_STATE_FILE`, written by `alerting_config.py`) names at least one receiver.
`ANDAI_ALERT_SLACK` / `ANDAI_ALERT_TEAMS` are read only by that renderer.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator

from andai_core.settings import Settings

# BOT-04: a canary on each platform at least every 4 hours.
MAX_CANARY_INTERVAL_S = 4 * 3600
# INV-01: a due scheduled meeting is dispatched within one tick of its start minus the lead.
MAX_SCHEDULED_TICK_S = 30.0
MAX_JOIN_LEAD_S = 15 * 60
DEFAULT_ALERTING_STATE_FILE = Path("/run/munshi-alerting/state.json")


class WorkerSettings(Settings):
    canary_meet_url: SecretStr | None = Field(default=None, validation_alias="ANDAI_CANARY_MEET_URL")
    canary_teams_url: SecretStr | None = Field(
        default=None, validation_alias="ANDAI_CANARY_TEAMS_URL"
    )
    canary_zoom_url: SecretStr | None = Field(default=None, validation_alias="ANDAI_CANARY_ZOOM_URL")
    canary_interval_s: int = Field(
        default=MAX_CANARY_INTERVAL_S,
        gt=0,
        le=MAX_CANARY_INTERVAL_S,
        validation_alias="ANDAI_CANARY_INTERVAL_S",
    )
    # The operator address join alerts go to; empty turns alert mail off.
    alert_email: str = Field(default="", validation_alias="ANDAI_ALERT_EMAIL")
    scheduled_tick_s: float = Field(
        default=15.0, gt=0, le=MAX_SCHEDULED_TICK_S, validation_alias="ANDAI_SCHEDULED_TICK_S"
    )
    join_lead_s: int = Field(
        default=120, ge=0, le=MAX_JOIN_LEAD_S, validation_alias="ANDAI_INTAKE_JOIN_LEAD_S"
    )
    # Monitoring (andai/docs/monitoring.md): Alertmanager runs; where the renderer records which
    # receivers it enabled; and which webhook receivers the operator turned on.
    monitoring: bool = Field(default=False, validation_alias="ANDAI_MONITORING")
    alerting_state_file: Path = Field(
        default=DEFAULT_ALERTING_STATE_FILE, validation_alias="ANDAI_ALERTING_STATE_FILE"
    )
    alert_slack: bool = Field(default=False, validation_alias="ANDAI_ALERT_SLACK")
    alert_teams: bool = Field(default=False, validation_alias="ANDAI_ALERT_TEAMS")

    @field_validator("alerting_state_file", mode="before")
    @classmethod
    def _blank_state_file_is_default(cls, value: object) -> object:
        return DEFAULT_ALERTING_STATE_FILE if value in (None, "") else value

    @property
    def canary_urls(self) -> dict[str, str]:
        """Platform → standing canary link, for the platforms that have one."""
        links = {
            "google_meet": self.canary_meet_url,
            "teams": self.canary_teams_url,
            "zoom": self.canary_zoom_url,
        }
        return {
            platform: secret.get_secret_value().strip()
            for platform, secret in links.items()
            if secret is not None and secret.get_secret_value().strip()
        }

    @property
    def alert_to(self) -> str | None:
        return self.alert_email.strip() or None


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
