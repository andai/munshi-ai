"""andai-intake settings: andai-core's settings class plus the notetaker mailbox's own.

The intake reads and writes the andai database as `andai_app` (`ANDAI_DB_URL_APP`), and only
through the `inbound_mail` functions of migration 0012, so it shares
`andai_core.settings.Settings` for the database keys.

* `ANDAI_INTAKE_ADDRESSES` — comma-separated notetaker addresses; mail for any other recipient is
  refused in the SMTP session (550). Empty refuses every recipient.
* `ANDAI_INTAKE_SMTP_BIND` — `host:port` the SMTP receiver listens on inside the container.
* `ANDAI_INTAKE_MAX_BYTES` — the largest message accepted (552 above it; default 10 MiB).
* `ANDAI_INTAKE_RAW_RETENTION_DAYS` / `ANDAI_INTAKE_ROW_RETENTION_DAYS` — the raw bytes are purged
  after 7 days and the whole row after 30 (D-58).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator

from andai_core.settings import Settings

MAX_RECIPIENTS = 5


class IntakeSettings(Settings):
    intake_addresses_raw: str = Field(default="", validation_alias="ANDAI_INTAKE_ADDRESSES")
    intake_smtp_bind: str = Field(default="0.0.0.0:2525", validation_alias="ANDAI_INTAKE_SMTP_BIND")
    intake_max_bytes: int = Field(
        default=10 * 1024 * 1024, ge=1024, validation_alias="ANDAI_INTAKE_MAX_BYTES"
    )
    intake_raw_retention_days: int = Field(
        default=7, ge=1, validation_alias="ANDAI_INTAKE_RAW_RETENTION_DAYS"
    )
    intake_row_retention_days: int = Field(
        default=30, ge=1, validation_alias="ANDAI_INTAKE_ROW_RETENTION_DAYS"
    )

    @field_validator("intake_smtp_bind")
    @classmethod
    def _host_port(cls, value: str) -> str:
        host, sep, port = value.strip().rpartition(":")
        if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError("ANDAI_INTAKE_SMTP_BIND must be host:port")
        return value.strip()

    @model_validator(mode="after")
    def _rows_outlive_raw(self) -> IntakeSettings:
        if self.intake_row_retention_days < self.intake_raw_retention_days:
            raise ValueError(
                "ANDAI_INTAKE_ROW_RETENTION_DAYS must not be shorter than"
                " ANDAI_INTAKE_RAW_RETENTION_DAYS"
            )
        return self

    @property
    def intake_addresses(self) -> frozenset[str]:
        """The notetaker addresses, lower-cased."""
        return frozenset(
            a.strip().lower() for a in self.intake_addresses_raw.split(",") if a.strip()
        )

    @property
    def smtp_listen(self) -> tuple[str, int]:
        host, _, port = self.intake_smtp_bind.rpartition(":")
        return host.strip("[]"), int(port)


@lru_cache(maxsize=1)
def get_intake_settings() -> IntakeSettings:
    return IntakeSettings()
