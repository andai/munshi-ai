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

    python -m andai_core canary-setup

`canary-setup` creates the canary org once: an org flagged `is_canary` with its own platform
principal (a cap of 3 bots, one per platform) and an owner that cannot sign in (no password, never
mailed). andai-worker sends every canary bot as this org, never as a customer org. It needs the KEK
(the principal's key is sealed under it); run again, it changes nothing. `make -C andai
canary-setup` runs it in the stack.

    python -m andai_core kek-rewrap [--batch 100] [--dry-run]
    python -m andai_core kek-status

Key rotation (andai/docs/runbooks/key-rotation.md). `kek-rewrap` moves every sealed value that is
not under the active KEK onto it (`andai_core.sealed.rewrap_all`): only the wrapped data key
changes, and it is safe to run again after a crash. `--dry-run` writes nothing and counts what
would move. `kek-status` prints how many values each KEK wraps per table and exits 1 while any
value is under a key other than the active one. Both need the KEK (active and retired) and print
counts and key ids only. `make -C andai kek-rewrap | kek-status` run them in the stack.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Engine, text, update
from sqlalchemy.exc import DBAPIError

from andai_core import sealed
from andai_core.auth.bootstrap import AlreadyBootstrapped, bootstrap
from andai_core.auth.links import InvalidEmail, canonical_email
from andai_core.crypto import Keyring, KeyUnavailable, load_keyring
from andai_core.db.models import Org
from andai_core.db.session import create_app_engine
from andai_core.mail import Mailer, MailError, SmtpMailer
from andai_core.meetings.service import canary_org_id
from andai_core.org.service import CreatedOrg, create_org
from andai_core.platform import principals
from andai_core.platform.admin_client import (
    HttpPlatformAdmin,
    PlatformAdmin,
    PlatformAdminError,
)
from andai_core.settings import Settings, get_settings

CANARY_ORG_NAME = "munshi.ai canary"
# The canary org's owner: a placeholder identity that never signs in and is never mailed.
CANARY_OWNER_EMAIL = "canary-bot@canary.munshi.test"
# One canary bot per platform at a time.
CANARY_MAX_CONCURRENT_BOTS = 3


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
    commands.add_parser(
        "canary-setup", help="create the canary org and its platform principal (once)"
    )
    rewrap = commands.add_parser(
        "kek-rewrap", help="move every sealed value onto the active KEK (key rotation)"
    )
    rewrap.add_argument(
        "--batch", type=int, default=sealed.DEFAULT_BATCH, help="rows per transaction"
    )
    rewrap.add_argument(
        "--dry-run", action="store_true", help="count what would move; write nothing"
    )
    commands.add_parser(
        "kek-status", help="sealed values per table and KEK; exit 1 while any is not on the active"
    )
    return parser


class _CanaryExists(Exception):
    pass


def _canary_setup(
    settings: Settings,
    engine: Engine | None,
    admin: PlatformAdmin | None,
    keyring: Keyring | None,
) -> int:
    try:
        keyring = keyring or load_keyring(settings)
    except KeyUnavailable as exc:
        print(f"canary-setup: {exc}", file=sys.stderr)
        return 2
    if keyring.ephemeral:
        print("canary-setup: ANDAI_KEK or ANDAI_KEK_FILE is required; nothing was created",
              file=sys.stderr)
        return 2
    admin = admin or HttpPlatformAdmin.from_settings(settings)
    own_engine = engine is None
    engine = engine or create_app_engine(settings=settings)

    def guard(tx) -> None:
        if tx.scalar(text("SELECT andai_canary_org_id()")) is not None:
            raise _CanaryExists

    def finish(tx, created: CreatedOrg) -> None:
        tx.execute(update(Org).where(Org.id == created.org_id).values(is_canary=True))

    try:
        existing = canary_org_id(engine)
        if existing is not None:
            print(f"canary-setup: the canary org exists ({existing}); nothing was changed")
            return 0
        created = create_org(
            engine,
            name=CANARY_ORG_NAME,
            owner_email=CANARY_OWNER_EMAIL,
            owner_display_name="munshi.ai canary",
            admin=admin,
            keyring=keyring,
            now=datetime.now(UTC),
            guard=guard,
            finish=finish,
            max_concurrent_bots=CANARY_MAX_CONCURRENT_BOTS,
        )
    except _CanaryExists:
        print("canary-setup: the canary org exists; nothing was changed")
        return 0
    except PlatformAdminError as exc:
        print(f"canary-setup: {exc}; nothing was created", file=sys.stderr)
        return 1
    except DBAPIError as exc:
        print(
            f"canary-setup: database error ({type(exc.orig).__name__}); is the andai database"
            " migrated to head (make -C andai db-migrate)?",
            file=sys.stderr,
        )
        return 1
    finally:
        if own_engine:
            engine.dispose()
    print(f"canary-setup: created the canary org ({created.org_id}) with its platform principal")
    return 0


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


def _rotation_keyring(
    command: str, settings: Settings, keyring: Keyring | None
) -> Keyring | None:
    try:
        keyring = keyring or load_keyring(settings)
    except KeyUnavailable as exc:
        print(f"{command}: {exc}", file=sys.stderr)
        return None
    if keyring.ephemeral:
        print(f"{command}: ANDAI_KEK or ANDAI_KEK_FILE is required", file=sys.stderr)
        return None
    return keyring


def _db_error(command: str, exc: DBAPIError) -> int:
    print(
        f"{command}: database error ({type(exc.orig).__name__}); is the andai database migrated"
        " to head (make -C andai db-migrate)?",
        file=sys.stderr,
    )
    return 1


def _key_label(keyring: Keyring, key_id: str) -> str:
    if key_id == keyring.key_id:
        return "active"
    return "retired" if key_id in keyring.retired_ids else "not loaded"


def _kek_rewrap(
    settings: Settings, engine: Engine | None, keyring: Keyring | None, batch: int, dry_run: bool
) -> int:
    command = "kek-rewrap"
    keyring = _rotation_keyring(command, settings, keyring)
    if keyring is None:
        return 2
    if batch < 1:
        print(f"{command}: --batch must be at least 1", file=sys.stderr)
        return 2
    own_engine = engine is None
    engine = engine or create_app_engine(settings=settings)
    try:
        result = sealed.rewrap_all(engine, keyring, batch=batch, dry_run=dry_run)
    except DBAPIError as exc:
        return _db_error(command, exc)
    finally:
        if own_engine:
            engine.dispose()
    verb = "would re-wrap" if dry_run else "re-wrapped"
    note = " (dry run: nothing written)" if dry_run else ""
    print(f"{command}: active key {keyring.key_id}{note}")
    failed = 0
    for table, count in result.items():
        print(
            f"{command}: {table}: {verb} {count.rewrapped}, skipped {count.skipped},"
            f" failed {count.failed}"
        )
        failed += count.failed
    return 1 if failed else 0


def _kek_status(settings: Settings, engine: Engine | None, keyring: Keyring | None) -> int:
    command = "kek-status"
    keyring = _rotation_keyring(command, settings, keyring)
    if keyring is None:
        return 2
    own_engine = engine is None
    engine = engine or create_app_engine(settings=settings)
    try:
        counts = sealed.counts_by_key(engine)
    except DBAPIError as exc:
        return _db_error(command, exc)
    finally:
        if own_engine:
            engine.dispose()
    retired = ", ".join(keyring.retired_ids) or "none"
    print(f"{command}: active key {keyring.key_id}; retired keys: {retired}")
    behind = 0
    for column in sealed.SEALED_COLUMNS:
        present = {k: n for (table, k), n in counts.items() if table == column.table}
        for key_id in (keyring.key_id, *keyring.retired_ids):
            present.setdefault(key_id, 0)
        for key_id in sorted(present, key=lambda k: (k != keyring.key_id, k)):
            n = present[key_id]
            print(f"{command}: {column.table}: {key_id} ({_key_label(keyring, key_id)}) {n}")
            if key_id != keyring.key_id:
                behind += n
    print(f"{command}: {behind} values are under a key other than the active key")
    return 1 if behind else 0


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
    if args.command == "canary-setup":
        return _canary_setup(settings, engine, admin, keyring)
    if args.command == "kek-rewrap":
        return _kek_rewrap(settings, engine, keyring, args.batch, args.dry_run)
    if args.command == "kek-status":
        return _kek_status(settings, engine, keyring)
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
