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

Sender verification (D-56, `andai_intake.auth`):

* `ANDAI_INTAKE_DNS_RESOLVER` — the DNS servers every SPF/DKIM/ARC/DMARC lookup goes to
  (comma-separated `host`, `host:port` or `[ipv6]:port`); empty uses the container's resolver.
* `ANDAI_INTAKE_DNS_TIMEOUT_S` — the cap on one lookup (default 3 s).
* `ANDAI_INTAKE_DNS_MAX_QUERIES` — lookups one message may cause (default 30); past it, `temperror`.
* `ANDAI_INTAKE_ARC_SEALERS` — comma-separated ARC sealer domains whose hop may vouch for a DMARC
  pass. It ships empty; a change needs the CTO.

Scheduling (D-58, `andai_intake.processor`, `andai_intake.horizon`):

* `ANDAI_INTAKE_HORIZON_DAYS` — how far ahead an invite's occurrences get scheduled meetings
  (default 14); the hourly top-up adds the rest as they come within it.
* `ANDAI_DEFAULT_TIMEZONE` (a floating invite time is read in it) and `ANDAI_DEFAULT_LANGUAGE` (the
  meeting's language) come from andai-core's settings.
* `ANDAI_INTAKE_JOIN_LEAD_S` — how long before the start andai-worker sends the bot (default 120);
  the confirmation says so.

Confirmations (INV-04, `andai_intake.confirm`) go through andai-core's SMTP settings (`SMTP_*`).

Rate limits (D-58, `andai_intake.limits`), per sliding hour:

* `ANDAI_INTAKE_RATE_SENDER_PER_H` (20) and `ANDAI_INTAKE_RATE_ORG_PER_H` (200) count verified mail
  only. `ANDAI_INTAKE_RATE_PEER_PER_H` (120) counts all mail per delivering IP.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from pydantic import Field, field_validator, model_validator

from andai_core.settings import Settings
from andai_intake.auth.dns import parse_nameservers

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
    intake_dns_resolver: str = Field(default="", validation_alias="ANDAI_INTAKE_DNS_RESOLVER")
    intake_dns_timeout_s: float = Field(
        default=3.0, gt=0, le=30, validation_alias="ANDAI_INTAKE_DNS_TIMEOUT_S"
    )
    intake_dns_max_queries: int = Field(
        default=30, ge=1, le=500, validation_alias="ANDAI_INTAKE_DNS_MAX_QUERIES"
    )
    intake_arc_sealers_raw: str = Field(default="", validation_alias="ANDAI_INTAKE_ARC_SEALERS")
    intake_rate_sender_per_h: int = Field(
        default=20, ge=1, validation_alias="ANDAI_INTAKE_RATE_SENDER_PER_H"
    )
    intake_rate_org_per_h: int = Field(
        default=200, ge=1, validation_alias="ANDAI_INTAKE_RATE_ORG_PER_H"
    )
    intake_rate_peer_per_h: int = Field(
        default=120, ge=1, validation_alias="ANDAI_INTAKE_RATE_PEER_PER_H"
    )
    intake_horizon_days: int = Field(
        default=14, ge=1, le=60, validation_alias="ANDAI_INTAKE_HORIZON_DAYS"
    )
    intake_join_lead_s: int = Field(
        default=120, ge=0, le=900, validation_alias="ANDAI_INTAKE_JOIN_LEAD_S"
    )

    @field_validator("intake_dns_resolver")
    @classmethod
    def _nameservers(cls, value: str) -> str:
        try:
            parse_nameservers(value)
        except ValueError as exc:
            raise ValueError(f"ANDAI_INTAKE_DNS_RESOLVER: {exc}") from None
        return value.strip()

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
    def dns_nameservers(self) -> tuple[tuple[str, int], ...]:
        return parse_nameservers(self.intake_dns_resolver)

    @property
    def arc_sealers(self) -> frozenset[str]:
        """The allowlisted ARC sealer domains, lower-case, without a trailing dot."""
        return frozenset(
            d.strip().rstrip(".").lower() for d in self.intake_arc_sealers_raw.split(",") if d.strip()
        )

    @property
    def horizon(self) -> timedelta:
        return timedelta(days=self.intake_horizon_days)

    @property
    def smtp_listen(self) -> tuple[str, int]:
        host, _, port = self.intake_smtp_bind.rpartition(":")
        return host.strip("[]"), int(port)


@lru_cache(maxsize=1)
def get_intake_settings() -> IntakeSettings:
    return IntakeSettings()
