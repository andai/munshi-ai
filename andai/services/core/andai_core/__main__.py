"""Operator commands: `python -m andai_core <command>`.

    python -m andai_core bootstrap --email owner@example.com --org "Example Ltd" [--name "A. Owner"]

`bootstrap` creates the first org (with its principal on the meeting platform) and its owner and
mails the owner a set-password link through the configured SMTP relay. It reads the same
environment as the service (`ANDAI_DB_URL_APP`, `ANDAI_KEK`, `PLATFORM_ADMIN_URL`,
`PLATFORM_ADMIN_TOKEN`, `SMTP_*`, `ANDAI_PORTAL_ORIGIN`) and refuses to run without a KEK: the
principal's key sealed under a one-off key would be unreadable. In the stack run it inside the
andai-core container:

    docker compose ... exec andai-core python -m andai_core bootstrap --email ... --org ...

    python -m andai_core principals-sync

`principals-sync` re-applies each existing org principal's platform settings (storage prefix
`org/<org_id>`, diagnostic tapes off, `ANDAI_ORG_MAX_CONCURRENT_BOTS`) through the admin-api. It is
idempotent, needs no KEK (no key is opened), and prints counts only. `make -C andai principals-sync`
runs it in the stack.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError

from andai_core.auth.bootstrap import AlreadyBootstrapped, bootstrap
from andai_core.auth.links import InvalidEmail, canonical_email
from andai_core.crypto import Keyring, KeyUnavailable, load_keyring
from andai_core.db.session import create_app_engine
from andai_core.mail import Mailer, MailError, SmtpMailer
from andai_core.platform import principals
from andai_core.platform.admin_client import (
    HttpPlatformAdmin,
    PlatformAdmin,
    PlatformAdminError,
)
from andai_core.settings import Settings, get_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m andai_core")
    commands = parser.add_subparsers(dest="command", required=True)
    boot = commands.add_parser("bootstrap", help="create the first org and its owner")
    boot.add_argument("--email", required=True, help="the owner's email address")
    boot.add_argument("--org", required=True, help="the org's display name")
    boot.add_argument("--name", default=None, help="the owner's display name")
    commands.add_parser(
        "principals-sync",
        help="re-apply every org principal's platform settings (storage prefix, tapes off, cap)",
    )
    return parser


def _principals_sync(settings: Settings, engine: Engine | None, admin: PlatformAdmin | None) -> int:
    admin = admin or HttpPlatformAdmin.from_settings(settings)
    own_engine = engine is None
    engine = engine or create_app_engine(settings=settings)
    try:
        result = principals.sync(
            engine, admin, max_concurrent_bots=settings.org_max_concurrent_bots
        )
    except DBAPIError as exc:
        print(
            f"principals-sync: database error ({type(exc.orig).__name__}); is the andai database"
            " migrated to head (make -C andai db-migrate)?",
            file=sys.stderr,
        )
        return 1
    finally:
        if own_engine:
            engine.dispose()
    print(
        f"principals-sync: {result.orgs} orgs with a principal, {result.configured} configured,"
        f" {result.failed} failed"
    )
    return 1 if result.failed else 0


def main(
    argv: Sequence[str] | None = None,
    *,
    engine: Engine | None = None,
    mailer: Mailer | None = None,
    admin: PlatformAdmin | None = None,
    keyring: Keyring | None = None,
) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    if args.command == "principals-sync":
        return _principals_sync(settings, engine, admin)
    try:
        keyring = keyring or load_keyring(settings)
    except KeyUnavailable as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 2
    if keyring.ephemeral:
        print(
            "bootstrap: ANDAI_KEK or ANDAI_KEK_FILE is required; nothing was created",
            file=sys.stderr,
        )
        return 2
    admin = admin or HttpPlatformAdmin.from_settings(settings)
    own_engine = engine is None
    engine = engine or create_app_engine(settings=settings)
    mailer = mailer or SmtpMailer(settings)
    try:
        email = canonical_email(args.email)
        done = bootstrap(
            engine,
            email=email,
            org_name=args.org,
            display_name=args.name,
            settings=settings,
            now=datetime.now(UTC),
            send=lambda mail: asyncio.run(mailer.send(mail)),
            admin=admin,
            keyring=keyring,
        )
    except InvalidEmail as exc:
        print(f"bootstrap: {exc}: {args.email!r}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 2
    except AlreadyBootstrapped:
        print("bootstrap: users already exist; nothing was changed", file=sys.stderr)
        return 1
    except MailError as exc:
        print(f"bootstrap: {exc}; nothing was created", file=sys.stderr)
        return 1
    except PlatformAdminError as exc:
        print(f"bootstrap: {exc}; nothing was created", file=sys.stderr)
        return 1
    finally:
        if own_engine:
            engine.dispose()
    print(
        f"bootstrap: created org {args.org.strip()!r} ({done.org_id}) with owner {email}"
        f" ({done.user_id}); the set-password link was mailed to {email}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
