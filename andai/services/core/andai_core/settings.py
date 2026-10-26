"""andai-core settings, read from the process environment.

Every value comes from the environment the compose overlay (or a test) provides; nothing secret has
a default. Secrets are `SecretStr`, so they never appear in a repr or a log line.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    # Deployment posture: `dev` relaxes what only a developer machine may relax (an ephemeral
    # KEK, non-Secure cookies). Anything else is treated as production.
    andai_env: str = Field(default="dev", validation_alias="ANDAI_ENV")

    # The application connects as the RLS-bound app role; the owner URL is used only by migrations.
    db_url_app: SecretStr | None = Field(default=None, validation_alias="ANDAI_DB_URL_APP")
    db_url_owner: SecretStr | None = Field(default=None, validation_alias="ANDAI_DB_URL_OWNER")

    # Key-encryption key: base64 of 32 bytes, inline or in a file (one of the two).
    kek: SecretStr | None = Field(default=None, validation_alias="ANDAI_KEK")
    kek_file: Path | None = Field(default=None, validation_alias="ANDAI_KEK_FILE")

    # The meeting platform's admin-api (per-org principal provisioning).
    platform_admin_url: str = Field(
        default="http://admin-api:8001", validation_alias="PLATFORM_ADMIN_URL"
    )
    platform_admin_token: SecretStr | None = Field(
        default=None, validation_alias="PLATFORM_ADMIN_TOKEN"
    )
    # How many bots one org may have in meetings at once (its principal's cap on the platform).
    org_max_concurrent_bots: int = Field(
        default=3, ge=0, validation_alias="ANDAI_ORG_MAX_CONCURRENT_BOTS"
    )

    # Outgoing mail (settings-only SMTP; Mailpit in development).
    smtp_host: str = Field(default="mailpit", validation_alias="SMTP_HOST")
    smtp_port: int = Field(default=1025, validation_alias="SMTP_PORT")
    smtp_user: str | None = Field(default=None, validation_alias="SMTP_USER")
    smtp_password: SecretStr | None = Field(default=None, validation_alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="munshi.ai <no-reply@munshi.invalid>", validation_alias="SMTP_FROM")
    smtp_starttls: bool = Field(default=False, validation_alias="SMTP_STARTTLS")

    redis_url: str = Field(default="redis://redis:6379/1", validation_alias="REDIS_URL")

    # The only origin allowed to make state-changing browser requests.
    portal_origin: str = Field(
        default="http://127.0.0.1:13100", validation_alias="ANDAI_PORTAL_ORIGIN"
    )
    # Comma-separated hostnames or CIDRs whose X-Forwarded-For is believed.
    trusted_proxies_raw: str = Field(default="", validation_alias="ANDAI_TRUSTED_PROXIES")

    # Session lifetimes: a session ends after this long without a request (idle) or this long
    # after login (absolute), whichever comes first.
    session_idle_seconds: int = Field(
        default=12 * 3600, gt=0, validation_alias="ANDAI_SESSION_IDLE_SECONDS"
    )
    session_max_seconds: int = Field(
        default=7 * 24 * 3600, gt=0, validation_alias="ANDAI_SESSION_MAX_SECONDS"
    )
    # Emailed links: a password-reset link lives 30 minutes, an invite (and the first owner's
    # set-password link) 7 days.
    reset_ttl_seconds: int = Field(
        default=30 * 60, gt=0, validation_alias="ANDAI_RESET_TTL_SECONDS"
    )
    invite_ttl_seconds: int = Field(
        default=7 * 24 * 3600, gt=0, validation_alias="ANDAI_INVITE_TTL_SECONDS"
    )

    @property
    def is_dev(self) -> bool:
        return self.andai_env.strip().lower() == "dev"

    @property
    def trusted_proxies(self) -> list[str]:
        return [p.strip() for p in self.trusted_proxies_raw.split(",") if p.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
