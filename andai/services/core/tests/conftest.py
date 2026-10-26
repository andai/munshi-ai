"""Throwaway infrastructure for andai-core tests.

Session-scoped disposable containers, started with `docker run` on random loopback ports and
removed at the end of the session; each starts only when a test asks for it:

* `postgres:17-alpine` — initialised with `migrations/db-init.sql` exactly as `make db-init`
  does (database `andai`, roles `andai_owner` and `andai_app`).
* `valkey/valkey:8-alpine`
* `axllent/mailpit:v1.32.0`

Without Docker the container-backed tests skip with a message; `ANDAI_REQUIRE_DOCKER=1` turns that
skip into a failure, so a green run with that variable set never hides a skipped test.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

CORE_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = CORE_DIR / "alembic.ini"
DB_INIT_SQL = CORE_DIR / "migrations" / "db-init.sql"

POSTGRES_IMAGE = "postgres:17-alpine"
VALKEY_IMAGE = "valkey/valkey:8-alpine"
MAILPIT_IMAGE = "axllent/mailpit:v1.32.0"
LABEL = "andai.test=core"


def _require_docker() -> None:
    reason = None
    if shutil.which("docker") is None:
        reason = "docker CLI not found"
    else:
        probe = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
        if probe.returncode != 0:
            reason = "docker daemon not reachable"
    if reason is None:
        return
    message = f"container-backed test needs Docker: {reason}"
    if os.environ.get("ANDAI_REQUIRE_DOCKER") == "1":
        pytest.fail(f"{message} (ANDAI_REQUIRE_DOCKER=1)", pytrace=False)
    pytest.skip(message)


def _docker(*args: str, stdin: bytes | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", *args],
        input=stdin,
        capture_output=True,
        check=False,
        env=None if env is None else {**os.environ, **env},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker {args[0]} failed ({result.returncode}): {result.stderr.decode(errors='replace')}"
        )
    return result.stdout.decode()


def _run_container(image: str, *args: str, ports: tuple[int, ...], env: dict[str, str]) -> str:
    cmd = ["run", "-d", "--rm", "--label", LABEL]
    for port in ports:
        cmd += ["-p", f"127.0.0.1::{port}"]
    for key in env:
        cmd += ["-e", key]  # value passed through the environment, not the command line
    return _docker(*cmd, image, *args, env=env).strip()


def _host_port(container: str, port: int) -> int:
    mapping = _docker("port", container, f"{port}/tcp").splitlines()[0]
    return int(mapping.rsplit(":", 1)[1])


def _wait(check, what: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception as exc:  # noqa: BLE001 - retried until the deadline
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f"{what} not ready after {timeout}s: {last!r}")


@dataclass(frozen=True)
class Postgres:
    container: str
    port: int
    superuser_password: str
    owner_password: str
    app_password: str

    def url(self, role: str, password: str, database: str = "andai") -> str:
        return f"postgresql+psycopg://{role}:{password}@127.0.0.1:{self.port}/{database}"

    @property
    def owner_url(self) -> str:
        return self.url("andai_owner", self.owner_password)

    @property
    def app_url(self) -> str:
        return self.url("andai_app", self.app_password)

    @property
    def superuser_url(self) -> str:
        return self.url("postgres", self.superuser_password)

    def db_init(self) -> None:
        _docker(
            "exec", "-i",
            "-e", "ANDAI_DB_PASSWORD_OWNER",
            "-e", "ANDAI_DB_PASSWORD_APP",
            self.container,
            "psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres", "-f", "-",
            stdin=DB_INIT_SQL.read_bytes(),
            env={
                "ANDAI_DB_PASSWORD_OWNER": self.owner_password,
                "ANDAI_DB_PASSWORD_APP": self.app_password,
            },
        )


@pytest.fixture(scope="session")
def postgres() -> Iterator[Postgres]:
    _require_docker()
    superuser_password = secrets.token_hex(16)
    container = _run_container(
        POSTGRES_IMAGE,
        ports=(5432,),
        env={"POSTGRES_PASSWORD": superuser_password, "TZ": "UTC", "PGTZ": "UTC"},
    )
    try:
        pg = Postgres(
            container=container,
            port=_host_port(container, 5432),
            superuser_password=superuser_password,
            owner_password=secrets.token_hex(16),
            app_password=secrets.token_hex(16),
        )

        def accepts_tcp() -> bool:
            engine = create_engine(pg.url("postgres", superuser_password, "postgres"))
            try:
                with engine.connect() as conn:
                    return conn.execute(text("SELECT 1")).scalar_one() == 1
            finally:
                engine.dispose()

        # The image's init-time server listens on the Unix socket only, so a TCP connection
        # succeeds only once the final server is up.
        _wait(accepts_tcp, "postgres")
        pg.db_init()
        yield pg
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


def alembic_config(url: str):
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def migrated(postgres: Postgres) -> Postgres:
    """The andai database at the head revision, migrated as the owner role."""
    from alembic import command

    command.upgrade(alembic_config(postgres.owner_url), "head")
    return postgres


@pytest.fixture(scope="session")
def app_engine(migrated: Postgres) -> Iterator[Engine]:
    from andai_core.db.session import create_app_engine

    engine = create_app_engine(migrated.app_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def owner_engine(migrated: Postgres) -> Iterator[Engine]:
    engine = create_engine(migrated.owner_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def superuser_engine(migrated: Postgres) -> Iterator[Engine]:
    """Bypasses RLS: for assertions about what is really stored, never for app behaviour."""
    engine = create_engine(migrated.superuser_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def valkey_url() -> Iterator[str]:
    _require_docker()
    container = _run_container(VALKEY_IMAGE, ports=(6379,), env={})
    try:
        port = _host_port(container, 6379)
        _wait(
            lambda: _docker("exec", container, "valkey-cli", "ping").strip() == "PONG", "valkey"
        )
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


@dataclass(frozen=True)
class Mailpit:
    smtp_host: str
    smtp_port: int
    api_url: str


@pytest.fixture(scope="session")
def mailpit() -> Iterator[Mailpit]:
    _require_docker()
    container = _run_container(MAILPIT_IMAGE, ports=(1025, 8025), env={})
    try:
        api_url = f"http://127.0.0.1:{_host_port(container, 8025)}"
        _wait(
            lambda: urllib.request.urlopen(f"{api_url}/api/v1/info", timeout=2).status == 200,
            "mailpit",
        )
        yield Mailpit("127.0.0.1", _host_port(container, 1025), api_url)
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


def new_user(engine: Engine) -> uuid.UUID:
    """Insert a user as the app role (users are global: no org context needed)."""
    from andai_core.db.models import User
    from andai_core.db.session import transaction

    user_id = uuid.uuid4()
    with transaction(engine) as tx:
        tx.add(User(id=user_id, email=f"{user_id.hex}@example.test", display_name="Test User"))
    return user_id


def bootstrap_org(engine: Engine, owner_id: uuid.UUID) -> uuid.UUID:
    """The bootstrap path: generate the org id, set app.org_id, insert org + owner membership."""
    from andai_core.db.models import Org, OrgMembership
    from andai_core.db.session import transaction

    org_id = uuid.uuid4()
    with transaction(engine, org_id=org_id) as tx:
        tx.add(Org(id=org_id, name=f"Org {org_id.hex[:8]}", slug=f"org-{org_id.hex}"))
        tx.flush()
        tx.add(OrgMembership(org_id=org_id, user_id=owner_id, role="owner"))
    return org_id


# ─── andai-core app harness ───────────────────────────────────────────────────────────────────────

PORTAL_ORIGIN = "http://portal.test"
CLIENT_IP = "198.51.100.10"
PASSWORD = "correct horse battery staple"


def make_settings(**overrides: str):
    """Settings for a test app: dev posture, a fixed portal origin; `overrides` use env names."""
    from andai_core.settings import Settings

    return Settings(**{"ANDAI_ENV": "dev", "ANDAI_PORTAL_ORIGIN": PORTAL_ORIGIN, **overrides})


class Clock:
    """A settable UTC clock for the services (session expiry, link expiry)."""

    def __init__(self) -> None:
        from datetime import UTC, datetime

        self.now = datetime.now(UTC)

    def __call__(self):
        return self.now

    def advance(self, **delta: float) -> None:
        from datetime import timedelta

        self.now += timedelta(**delta)


@dataclass
class Harness:
    services: object
    mailer: object
    clock: Clock

    def client(self, ip: str = CLIENT_IP, origin: str | None = PORTAL_ORIGIN):
        from fastapi.testclient import TestClient

        from andai_core.main import create_app

        headers = {} if origin is None else {"Origin": origin}
        return TestClient(create_app(self.services), client=(ip, 50000), headers=headers)


def make_harness(engine: Engine, **settings: str) -> Harness:
    from andai_core.auth.clientip import ClientIpResolver
    from andai_core.auth.deps import Services
    from andai_core.auth.mfa import MFA_FAILURE_LIMIT, MFA_FAILURE_WINDOW_SECONDS
    from andai_core.auth.ratelimit import MemoryLimiter
    from andai_core.crypto import KEY_BYTES, Keyring
    from andai_core.mail import MemoryMailer

    clock = Clock()
    mailer = MemoryMailer()
    config = make_settings(**settings)
    services = Services(
        settings=config,
        engine=engine,
        login_limiter=MemoryLimiter(),
        reset_limiter=MemoryLimiter(),
        mfa_limiter=MemoryLimiter(MFA_FAILURE_LIMIT, MFA_FAILURE_WINDOW_SECONDS),
        mailer=mailer,
        ip_resolver=ClientIpResolver(config.trusted_proxies),
        keyring=Keyring(os.urandom(KEY_BYTES)),
        clock=clock,
    )
    return Harness(services=services, mailer=mailer, clock=clock)


@pytest.fixture
def harness(app_engine: Engine) -> Harness:
    return make_harness(app_engine)


@dataclass(frozen=True)
class Member:
    org_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    password: str


def new_member(
    engine: Engine,
    *,
    password: str | None = PASSWORD,
    status: str = "active",
    role: str = "owner",
    org_id: uuid.UUID | None = None,
) -> Member:
    """A user with an argon2id password in an org (a new org unless `org_id` is given)."""
    from andai_core.auth.passwords import hash_password
    from andai_core.db.models import Org, OrgMembership, User
    from andai_core.db.session import transaction

    user_id = uuid.uuid4()
    email = f"{user_id.hex}@example.test"
    with transaction(engine) as tx:
        tx.add(
            User(
                id=user_id,
                email=email,
                display_name="Test Member",
                status=status,
                password_hash=None if password is None else hash_password(password),
            )
        )
    new_org = org_id is None
    org_id = org_id or uuid.uuid4()
    with transaction(engine, org_id=org_id) as tx:
        if new_org:
            tx.add(Org(id=org_id, name=f"Org {org_id.hex[:8]}", slug=f"org-{org_id.hex}"))
            tx.flush()
        tx.add(OrgMembership(org_id=org_id, user_id=user_id, role=role))
    return Member(org_id=org_id, user_id=user_id, email=email, password=password or "")


@pytest.fixture
def fresh_app_engine(postgres: Postgres, migrated: Postgres) -> Iterator[Engine]:
    """An app-role engine on a brand-new database at head (for tests that need an empty one)."""
    from alembic import command

    name = f"andai_{uuid.uuid4().hex[:12]}"
    admin = create_engine(postgres.url("postgres", postgres.superuser_password, "postgres"))
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CREATE DATABASE {name} OWNER andai_owner"))
        conn.execute(text(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC"))
        conn.execute(text(f"GRANT CONNECT ON DATABASE {name} TO andai_owner, andai_app"))
    db_admin = create_engine(postgres.url("postgres", postgres.superuser_password, name))
    with db_admin.begin() as conn:
        conn.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO andai_app"))
    db_admin.dispose()
    command.upgrade(
        alembic_config(postgres.url("andai_owner", postgres.owner_password, name)), "head"
    )
    from andai_core.db.session import create_app_engine

    engine = create_app_engine(postgres.url("andai_app", postgres.app_password, name))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


def link_token(text_body: str, page: str) -> str:
    """The token of the `/<page>?token=` link in a mail body."""
    import re

    match = re.search(rf"{re.escape(PORTAL_ORIGIN)}/{page}\?token=([A-Za-z0-9_-]+)", text_body)
    assert match, f"no /{page} link in the mail"
    return match.group(1)


def invite_user(
    engine: Engine,
    harness: Harness,
    *,
    org_id: uuid.UUID,
    email: str,
    role: str = "member",
    invited_by: uuid.UUID | None = None,
) -> str:
    """The internal invite path (the admin route arrives with team management): create the invite
    in the org's transaction and return its token."""
    from andai_core.auth.invites import create_invite
    from andai_core.db.session import transaction

    with transaction(engine, org_id=org_id) as tx:
        return create_invite(
            tx,
            org_id=org_id,
            email=email,
            role=role,
            invited_by=invited_by,
            settings=harness.services.settings,
            now=harness.clock(),
        )


def mailpit_messages(api_url: str, to: str, *, expect: int = 1, timeout: float = 15.0) -> list:
    """Messages Mailpit holds for `to` (full bodies), waiting until at least `expect` arrived."""
    import json

    deadline = time.monotonic() + timeout
    found: list = []
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{api_url}/api/v1/messages?limit=500", timeout=5) as r:
            listing = json.load(r)
        ids = [
            m["ID"]
            for m in listing.get("messages", [])
            if any(a.get("Address", "").lower() == to.lower() for a in m.get("To", []))
        ]
        if len(ids) >= expect:
            found = []
            for message_id in ids:
                with urllib.request.urlopen(f"{api_url}/api/v1/message/{message_id}", timeout=5) as r:
                    found.append(json.load(r))
            return found
        time.sleep(0.3)
    return found


# ─── two-factor helpers ───────────────────────────────────────────────────────────────────────────

LOGIN_PATH = "/api/v1/auth/login"
MFA_VERIFY_PATH = "/api/v1/auth/mfa/verify"
MFA_PATH = "/api/v1/me/mfa"


def totp_code(totp, clock: Clock, steps: int = 0) -> str:
    """The code of `totp` at the harness clock, `steps` 30-second steps away."""
    return totp.generate_otp(totp.timecode(clock()) + steps)


def enroll(client, clock: Clock):
    """Enroll the signed-in user of `client`; returns (pyotp.TOTP, recovery codes). The clock moves
    on one step afterwards, so the confirming code's step is spent."""
    import pyotp

    start = client.post(MFA_PATH)
    assert start.status_code == 200, start.text
    totp = pyotp.parse_uri(start.json()["otpauth_uri"])
    confirm = client.post(f"{MFA_PATH}/confirm", json={"code": totp_code(totp, clock)})
    assert confirm.status_code == 200, confirm.text
    clock.advance(seconds=30)
    return totp, confirm.json()["recovery_codes"]


def audit_actions(superuser_engine: Engine, user_id: uuid.UUID) -> list[str]:
    """The audit actions whose target is `user_id`, oldest first."""
    with superuser_engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT action FROM audit_events WHERE target = :t ORDER BY at, action"),
                {"t": f"user:{user_id}"},
            ).scalars()
        )


def staged_session(engine: Engine, member: Member, stage: str, clock: Clock) -> str:
    """A session token in `stage` for `member`, inserted directly (not through login)."""
    from datetime import timedelta

    from andai_core.auth import sessions
    from andai_core.db.session import transaction

    lifetimes = sessions.Lifetimes(idle=timedelta(hours=12), absolute=timedelta(days=7))
    with transaction(engine) as tx:
        return sessions.create(
            tx,
            user_id=member.user_id,
            org_id=member.org_id,
            stage=stage,
            lifetimes=lifetimes,
            now=clock(),
        )


def sign_in(harness: Harness, member: Member, totp=None):
    """A client with a `full` session for `member` (through the code step if `totp` is given)."""
    client = harness.client()
    stage = client.post(
        LOGIN_PATH, json={"email": member.email, "password": member.password}
    ).json()["stage"]
    if totp is not None:
        assert stage == "mfa_pending"
        verified = client.post(MFA_VERIFY_PATH, json={"code": totp_code(totp, harness.clock)})
        assert verified.json() == {"stage": "full"}
        harness.clock.advance(seconds=30)
    else:
        assert stage == "full"
    return client


def enrolled_member(harness: Harness, engine: Engine, **kwargs):
    member = new_member(engine, **kwargs)
    totp, _ = enroll(sign_in(harness, member), harness.clock)
    return member, totp


def session_count(superuser_engine: Engine, user_id: uuid.UUID) -> int:
    with superuser_engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM sessions WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()


# ─── orgs with every role ─────────────────────────────────────────────────────────────────────────


def new_team(
    engine: Engine, org_id: uuid.UUID, members: dict[uuid.UUID, str], name: str | None = None
) -> uuid.UUID:
    """A team in `org_id` with `members` ({user_id: "lead" | "member"}), inserted directly."""
    from andai_core.db.models import Team, TeamMembership
    from andai_core.db.session import transaction

    team_id = uuid.uuid4()
    with transaction(engine, org_id=org_id) as tx:
        tx.add(Team(id=team_id, org_id=org_id, name=name or f"Team {team_id.hex[:8]}"))
        tx.flush()
        tx.add_all(
            TeamMembership(team_id=team_id, org_id=org_id, user_id=user_id, role=role)
            for user_id, role in members.items()
        )
    return team_id


@dataclass(frozen=True)
class RoleOrg:
    """An org with an owner, an admin, a member, a lead (org role member) of `team`, and a
    second team `other_team` led by nobody the roles above know."""

    owner: Member
    admin: Member
    member: Member
    lead: Member
    team: uuid.UUID
    other_team: uuid.UUID

    def by_role(self, role: str) -> Member:
        return getattr(self, role)


ROLES = ("owner", "admin", "member", "lead")


def role_org(engine: Engine) -> RoleOrg:
    owner = new_member(engine)
    admin = new_member(engine, role="admin", org_id=owner.org_id)
    member = new_member(engine, role="member", org_id=owner.org_id)
    lead = new_member(engine, role="member", org_id=owner.org_id)
    team = new_team(engine, owner.org_id, {lead.user_id: "lead", member.user_id: "member"})
    other_team = new_team(engine, owner.org_id, {member.user_id: "member"})
    return RoleOrg(owner, admin, member, lead, team, other_team)


def served(app) -> set[tuple[str, str]]:
    """Every (method, path template) `app` serves, included routers flattened (an included router
    is one entry in `app.routes`)."""
    from fastapi.routing import iter_route_contexts

    return {
        (method, context.path)
        for context in iter_route_contexts(app.routes)
        for method in context.methods or ()
    }
