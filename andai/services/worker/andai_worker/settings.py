"""andai-worker settings: andai-core's settings class plus the canary and alert settings.

The worker reads the andai database as `andai_app` (`ANDAI_DB_URL_APP`), unseals the canary org's
platform key with the same KEK (`ANDAI_KEK`), dispatches through the platform gateway and mails
through the same SMTP relay as andai-core, so it shares `andai_core.settings.Settings`.

Canary links are standing test meetings that admit guests without a human (one per platform; an
empty value turns that platform's canary off). A Zoom link may carry its passcode, so the links are
`SecretStr` and never logged.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr

from andai_core.settings import Settings

# BOT-04: a canary on each platform at least every 4 hours.
MAX_CANARY_INTERVAL_S = 4 * 3600


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
