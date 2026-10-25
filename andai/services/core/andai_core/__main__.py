"""Operator commands: `python -m andai_core <command>`.

    python -m andai_core bootstrap --email owner@example.com --org "Example Ltd" [--name "A. Owner"]

`bootstrap` creates the first org and its owner and mails the owner a set-password link through
the configured SMTP relay. It reads the same environment as the service (`ANDAI_DB_URL_APP`,
`SMTP_*`, `ANDAI_PORTAL_ORIGIN`); in the stack run it inside the andai-core container:

    docker compose ... exec andai-core python -m andai_core bootstrap --email ... --org ...
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Engine

from andai_core.auth.bootstrap import AlreadyBootstrapped, bootstrap
from andai_core.auth.links import InvalidEmail, canonical_email
from andai_core.db.session import create_app_engine
from andai_core.mail import Mailer, MailError, SmtpMailer
from andai_core.settings import get_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m andai_core")
    commands = parser.add_subparsers(dest="command", required=True)
    boot = commands.add_parser("bootstrap", help="create the first org and its owner")
    boot.add_argument("--email", required=True, help="the owner's email address")
    boot.add_argument("--org", required=True, help="the org's display name")
    boot.add_argument("--name", default=None, help="the owner's display name")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    engine: Engine | None = None,
    mailer: Mailer | None = None,
) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
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
