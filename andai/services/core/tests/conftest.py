"""Throwaway infrastructure for andai-core tests.

Session-scoped disposable containers, started with `docker run` on random loopback ports and
removed at the end of the session; each starts only when a test asks for it:

* `postgres:17-alpine` — initialised with `migrations/db-init.sql` exactly as `make db-init`
  does (database `andai`, roles `andai_owner`, `andai_app` and the NOLOGIN `andai_scan`).
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
    """Settings for a test app: dev posture, a fixed portal origin, no background reconciler;
    `overrides` use env names."""
    from andai_core.settings import Settings

    return Settings(
        **{
            "ANDAI_ENV": "dev",
            "ANDAI_PORTAL_ORIGIN": PORTAL_ORIGIN,
            "ANDAI_RECONCILER": "off",
            **overrides,
        }
    )


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


class FakeAdminApi:
    """The meeting platform's admin-api, in memory, behind an `httpx.MockTransport`: the calls
    `HttpPlatformAdmin` makes, with the real service's answers (create user is idempotent by email
    with 201/200; tokens are minted per call and revocable; PATCH merges `data` into the user's
    record like the real one, `diagnostics` key by key). `fail` names calls to answer 500."""

    ADMIN_KEY = "test-admin-key-" + secrets.token_hex(8)

    def __init__(self) -> None:
        self.users: dict[str, int] = {}
        self.tokens: dict[int, dict] = {}
        self.records: dict[int, dict] = {}  # user id -> {"data", "max_concurrent_bots"}
        self.patches: list[tuple[int, dict]] = []
        self.calls: list[tuple[str, str]] = []
        self.fail: set[str] = set()
        self._ids = iter(range(1000, 10**9))

    def handler(self, request):
        import json
        import re

        import httpx

        path = request.url.path
        self.calls.append((request.method, path))
        if request.headers.get("x-admin-api-key") != self.ADMIN_KEY:
            return httpx.Response(403, json={"detail": "Invalid or missing admin token."})
        if request.method == "POST" and path == "/admin/users":
            if "create user" in self.fail:
                return httpx.Response(500, json={"detail": "boom"})
            email = json.loads(request.content)["email"].lower()
            created = email not in self.users
            if created:
                self.users[email] = next(self._ids)
            return httpx.Response(
                201 if created else 200, json={"id": self.users[email], "email": email}
            )
        if request.method == "POST" and re.fullmatch(r"/admin/users/\d+/tokens", path):
            if "mint token" in self.fail:
                return httpx.Response(500, json={"detail": "boom"})
            user_id = int(path.split("/")[3])
            if user_id not in self.users.values():
                return httpx.Response(404, json={"detail": "User not found"})
            scopes = request.url.params["scopes"].split(",")
            token_id = next(self._ids)
            token = f"tok_bot_{secrets.token_urlsafe(24)}"
            self.tokens[token_id] = {"token": token, "user_id": user_id, "scopes": scopes}
            return httpx.Response(
                201, json={"id": token_id, "token": token, "user_id": user_id, "scopes": scopes}
            )
        if request.method == "PATCH" and re.fullmatch(r"/admin/users/\d+", path):
            if "update user" in self.fail:
                return httpx.Response(500, json={"detail": "boom"})
            user_id = int(path.rsplit("/", 1)[1])
            if user_id not in self.users.values():
                return httpx.Response(404, json={"detail": "User not found"})
            body = json.loads(request.content)
            self.patches.append((user_id, body))
            record = self.records.setdefault(user_id, {"data": {}, "max_concurrent_bots": 3})
            data = dict(body.get("data") or {})
            if isinstance(data.get("diagnostics"), dict):
                data["diagnostics"] = {
                    **record["data"].get("diagnostics", {}), **data["diagnostics"]
                }
            record["data"] = {**record["data"], **data}
            if body.get("max_concurrent_bots") is not None:
                record["max_concurrent_bots"] = body["max_concurrent_bots"]
            return httpx.Response(200, json={"id": user_id, **record})
        if request.method == "DELETE" and path.startswith("/admin/tokens/"):
            if self.tokens.pop(int(path.rsplit("/", 1)[1]), None) is None:
                return httpx.Response(404, json={"detail": "Token not found"})
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "Not Found"})

    def client(self):
        import httpx
        from pydantic import SecretStr

        from andai_core.platform.admin_client import HttpPlatformAdmin

        return HttpPlatformAdmin(
            "http://admin-api.test",
            SecretStr(self.ADMIN_KEY),
            transport=httpx.MockTransport(self.handler),
        )


class FakeGateway:
    """The meeting platform's gateway, in memory, behind an `httpx.MockTransport`: the calls
    `HttpPlatformGateway` makes, answered like the real one. Keys are the tokens `FakeAdminApi`
    minted, so each org's principal sees only its own meetings. `POST /bots` parses the link the
    way the platform does for the three families, refuses a second live bot for the same
    (principal, platform, native id) with the structured 409, and answers 201 with status
    `requested`. Tests move a meeting on with `set` / `set_data`, and give it transcript segments
    with `transcripts[id]` (served by `GET /transcripts/by-id/{id}` as a TranscriptionResponse) and
    calendar invitees with `invitees[id]`. `GET /meetings/{platform}/{native}/participants` answers
    like meeting-api: the principal's newest meeting with that native id, its invitees
    (`source: invite`) and then the distinct speakers of its transcript (`source: speaker`), not
    de-duplicated. `DELETE /meetings/{id}` deletes a terminal meeting's transcript and stored
    objects (`objects[id]`) and keeps the row as a tombstone, like meeting-api: 404 for a meeting
    the principal does not own, 409 while it is live, 204 otherwise (again after a delete);
    `deletions` lists the ids in order. `fail` maps a call ("dispatch", "list bots",
    "get meeting", "stop", "get transcript", "get participants", "delete meeting") to a status to
    answer; `dispatch_delay` stalls dispatch."""

    LIVE = ("requested", "joining", "awaiting_admission", "active", "needs_human_help", "stopping")

    def __init__(self, admin_api: FakeAdminApi) -> None:
        self.admin_api = admin_api
        self.meetings: dict[int, dict] = {}
        self.owners: dict[int, int] = {}  # platform meeting id -> platform user id
        self.dispatches: list[dict] = []  # request bodies, in order
        self.stops: list[tuple[str, str]] = []
        self.transcripts: dict[int, list[dict]] = {}  # platform meeting id -> raw segments
        self.invitees: dict[int, list[dict]] = {}  # platform meeting id -> {name, email}
        self.objects: dict[int, list[str]] = {}  # platform meeting id -> stored object keys
        self.deletions: list[int] = []
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, int] = {}
        self.dispatch_delay = 0.0
        self._ids = iter(range(5000, 10**9))
        import threading

        self._lock = threading.Lock()

    def _user(self, request) -> int | None:
        key = request.headers.get("x-api-key")
        for token in self.admin_api.tokens.values():
            if token["token"] == key:
                return token["user_id"]
        return None

    @staticmethod
    def parse(url: str) -> tuple[str, str] | None:
        import re
        from urllib.parse import unquote, urlsplit

        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host == "meet.google.com":
            code = parts.path.strip("/").lower()
            return ("google_meet", code) if re.fullmatch(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", code) else None
        if "zoom" in host:
            m = re.search(r"\d{9,11}", parts.path)
            return ("zoom", m.group(0)) if m else None
        if host.endswith("teams.microsoft.com") or host.endswith("teams.live.com"):
            thread = re.search(r"19:meeting_[^/@]+@thread\.v2", unquote(url))
            if thread:
                return ("teams", thread.group(0))
            short = re.search(r"/meet/(\d+)", parts.path)
            return ("teams", short.group(1)) if short else None
        return None

    def set(self, meeting_id: int, **fields) -> None:
        from datetime import UTC, datetime

        with self._lock:
            self.meetings[meeting_id].update(fields)
            self.meetings[meeting_id]["updated_at"] = datetime.now(UTC).isoformat()

    def set_data(self, meeting_id: int, **data) -> None:
        with self._lock:
            self.meetings[meeting_id]["data"].update(data)

    def by_native(self, native: str) -> dict:
        [found] = [m for m in self.meetings.values() if m["native_meeting_id"] == native]
        return found

    def handler(self, request):
        import json
        import re
        import time
        from datetime import UTC, datetime
        from urllib.parse import unquote

        import httpx

        path = request.url.path
        self.calls.append((request.method, path))
        user = self._user(request)
        if user is None:
            return httpx.Response(401, json={"detail": "Invalid API key"})
        if request.method == "POST" and path == "/bots":
            if "dispatch" in self.fail:
                return httpx.Response(self.fail["dispatch"], json={"detail": "refused"})
            body = json.loads(request.content)
            if self.dispatch_delay:
                time.sleep(self.dispatch_delay)
            parsed = self.parse(body.get("meeting_url") or "")
            if parsed is None:
                return httpx.Response(422, json={"detail": "unrecognized meeting link"})
            platform, native = parsed
            with self._lock:
                self.dispatches.append(body)
                for mid, m in self.meetings.items():
                    if (
                        self.owners[mid] == user
                        and m["platform"] == platform
                        and m["native_meeting_id"] == native
                        and m["status"] in self.LIVE
                    ):
                        return httpx.Response(
                            409,
                            json={
                                "detail": "An active or requested meeting already exists",
                                "code": "duplicate_meeting",
                                "meeting_id": mid,
                                "platform": platform,
                                "native_meeting_id": native,
                            },
                        )
                mid = next(self._ids)
                now = datetime.now(UTC).isoformat()
                self.meetings[mid] = {
                    "id": mid,
                    "user_id": user,
                    "platform": platform,
                    "native_meeting_id": native,
                    "constructed_meeting_url": body.get("meeting_url"),
                    "status": "requested",
                    "bot_container_id": None,
                    "start_time": None,
                    "end_time": None,
                    "completion_reason": None,
                    "failure_stage": None,
                    "data": {},
                    "created_at": now,
                    "updated_at": now,
                }
                self.owners[mid] = user
                return httpx.Response(201, json=self.meetings[mid])
        if request.method == "GET" and path == "/bots":
            if "list bots" in self.fail:
                return httpx.Response(self.fail["list bots"], json={"detail": "boom"})
            limit = int(request.url.params.get("limit", 50))
            with self._lock:
                mine = [m for mid, m in self.meetings.items() if self.owners[mid] == user]
            mine.sort(key=lambda m: (m["status"] in self.LIVE, m["id"]), reverse=True)
            return httpx.Response(
                200, json={"meetings": mine[:limit], "has_more": len(mine) > limit}
            )
        if request.method == "GET" and re.fullmatch(r"/meetings/\d+", path):
            if "get meeting" in self.fail:
                return httpx.Response(self.fail["get meeting"], json={"detail": "boom"})
            mid = int(path.rsplit("/", 1)[1])
            if self.owners.get(mid) != user:
                return httpx.Response(404, json={"detail": "Meeting not found"})
            return httpx.Response(200, json=self.meetings[mid])
        if request.method == "GET" and re.fullmatch(r"/transcripts/by-id/\d+", path):
            if "get transcript" in self.fail:
                return httpx.Response(self.fail["get transcript"], json={"detail": "boom"})
            mid = int(path.rsplit("/", 1)[1])
            if self.owners.get(mid) != user:
                return httpx.Response(404, json={"detail": f"Meeting {mid} not found"})
            m = self.meetings[mid]
            return httpx.Response(
                200,
                json={
                    "id": mid,
                    "platform": m["platform"],
                    "native_meeting_id": m["native_meeting_id"],
                    "constructed_meeting_url": m["constructed_meeting_url"],
                    "status": m["status"],
                    "start_time": m["start_time"],
                    "end_time": m["end_time"],
                    "recordings": [],
                    "notes": None,
                    "data": {},
                    "segments": list(self.transcripts.get(mid, [])),
                },
            )
        participants = re.fullmatch(r"/meetings/([^/]+)/([^/]+)/participants", path)
        if request.method == "GET" and participants:
            if "get participants" in self.fail:
                return httpx.Response(self.fail["get participants"], json={"detail": "boom"})
            platform, native = unquote(participants.group(1)), unquote(participants.group(2))
            with self._lock:
                owned = [
                    mid for mid, m in self.meetings.items()
                    if self.owners[mid] == user and m["platform"] == platform
                    and m["native_meeting_id"] == native
                ]
            if not owned:
                return httpx.Response(404, json={"detail": "Meeting not found"})
            mid = max(owned)
            rows = [
                {"name": i.get("name"), "email": i.get("email"), "source": "invite"}
                for i in self.invitees.get(mid, [])
            ]
            speakers: list[str] = []
            for segment in self.transcripts.get(mid, []):
                name = segment.get("speaker")
                if isinstance(name, str) and name.strip() and name not in speakers:
                    speakers.append(name)
            rows += [{"name": name, "email": None, "source": "speaker"} for name in speakers]
            return httpx.Response(
                200,
                json={
                    "meeting_id": mid,
                    "platform": platform,
                    "native_meeting_id": native,
                    "participants": rows,
                    "sources": sorted({r["source"] for r in rows}),
                    "observed_roster": "not_recorded",
                },
            )
        if request.method == "DELETE" and re.fullmatch(r"/meetings/\d+", path):
            if "delete meeting" in self.fail:
                return httpx.Response(self.fail["delete meeting"], json={"detail": "boom"})
            mid = int(path.rsplit("/", 1)[1])
            with self._lock:
                if self.owners.get(mid) != user:
                    return httpx.Response(404, json={"detail": "Meeting not found"})
                m = self.meetings[mid]
                if m["status"] not in ("completed", "failed"):
                    return httpx.Response(
                        409,
                        json={"detail": "Meeting artifacts can only be deleted after the"
                              " lifecycle is terminal"},
                    )
                self.deletions.append(mid)
                self.transcripts.pop(mid, None)
                self.objects.pop(mid, None)
                m["data"] = {"artifact_deletion": {"state": "completed"}}
            return httpx.Response(204)
        if request.method == "DELETE" and path.startswith("/bots/"):
            if "stop" in self.fail:
                return httpx.Response(self.fail["stop"], json={"detail": "boom"})
            _, _, platform, native = path.split("/", 3)
            native = unquote(native)
            with self._lock:
                live = [
                    m for mid, m in self.meetings.items()
                    if self.owners[mid] == user and m["platform"] == platform
                    and m["native_meeting_id"] == native and m["status"] in self.LIVE
                ]
                if not live:
                    return httpx.Response(404, json={"detail": "No active meeting for this bot"})
                self.stops.append((platform, native))
                for m in live:
                    if m["status"] == "active":
                        m["status"] = "stopping"
                    else:
                        m["status"], m["completion_reason"] = "completed", "stopped_before_admission"
            return httpx.Response(202, json={"message": "Stop requested"})
        return httpx.Response(404, json={"detail": "Not Found"})

    def client(self):
        import httpx

        from andai_core.platform.gateway_client import HttpPlatformGateway

        return HttpPlatformGateway(
            "http://gateway.test", transport=httpx.MockTransport(self.handler)
        )


@dataclass
class Harness:
    services: object
    mailer: object
    clock: Clock
    admin_api: FakeAdminApi
    gateway: FakeGateway

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
    admin_api = FakeAdminApi()
    gateway = FakeGateway(admin_api)
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
        platform_admin=admin_api.client(),
        clock=clock,
        gateway=gateway.client(),
    )
    return Harness(
        services=services, mailer=mailer, clock=clock, admin_api=admin_api, gateway=gateway
    )


def give_principal(harness: Harness, engine: Engine, org_id: uuid.UUID) -> str:
    """Provision and store the org's platform principal through the fake admin-api; returns the
    key in clear (tests only)."""
    from andai_core.db.session import transaction
    from andai_core.platform import principals

    minted = principals.provision(harness.services.platform_admin, org_id)
    with transaction(engine, org_id=org_id) as tx:
        principals.store(tx, harness.services.keyring, org_id, minted, harness.clock())
    return minted.key


def zoom_link(scenario: str | None = None, *, host: str = "zoom.us", passcode: str = "s3cretPwd") -> str:
    """A Zoom-shaped link with a meeting number unique to this call (uuid-derived), so dedup and
    the removal window never couple two tests or two runs; `scenario` rides `mock=` only."""
    number = str(uuid.uuid4().int)[-11:].lstrip("0").rjust(11, "1")
    query = f"pwd={passcode}" + (f"&mock={scenario}" if scenario else "")
    return f"https://{host}/j/{number}?{query}"


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
        conn.execute(text("GRANT USAGE ON SCHEMA public TO andai_app, andai_scan"))
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


# ─── the nine callers of a meeting (Phase 5 access matrix) ───────────────────────────────────────

CALLERS = (
    "requester",
    "lead",
    "team_member",
    "user_grantee",
    "team_grantee",
    "admin",
    "owner",
    "unrelated",
    "other_org",
)
# Per AUTH_MATRIX, for a meeting the requester sent for `team`, shared with `user_grantee` and with
# `shared_team` (whose member is `team_grantee`).
READERS = frozenset({"requester", "lead", "user_grantee", "team_grantee", "admin", "owner"})
STOPPERS = frozenset({"requester", "lead", "admin", "owner"})
SHARERS = STOPPERS
DELETERS = frozenset({"requester", "admin", "owner"})


def seed_meeting(
    engine: Engine,
    org_id: uuid.UUID,
    requester: uuid.UUID,
    team: uuid.UUID | None,
    *,
    status: str = "done",
    shares: tuple[tuple[str, uuid.UUID], ...] | list = (),
    started_at=None,
    ended_at=None,
    created_at=None,
    title: str | None = None,
) -> uuid.UUID:
    """A meeting inserted directly (no platform), with `shares` ((grantee_type, grantee_id)); the
    title is written through `set_title`, so it is searchable."""
    from andai_core.db.models import Meeting, MeetingShare
    from andai_core.db.session import transaction
    from andai_core.meetings.title import set_title

    meeting_id = uuid.uuid4()
    with transaction(engine, org_id=org_id) as tx:
        meeting = Meeting(
            id=meeting_id,
            org_id=org_id,
            team_id=team,
            requested_by=requester,
            platform="zoom",
            join_url=f"https://zoom.us/j/{meeting_id.int % 10**11}?pwd=x",
            join_url_key=f"zoom.us/j/{meeting_id.hex}",
            status=status,
            failure_reason="internal_error" if status == "failed" else None,
            settings_snapshot={"bot_name": "munshi.ai (recording)"},
            started_at=started_at,
            ended_at=ended_at,
            **({} if created_at is None else {"created_at": created_at}),
        )
        set_title(tx, meeting, title)
        tx.flush()
        for grantee_type, grantee_id in shares:
            tx.add(
                MeetingShare(
                    org_id=org_id,
                    meeting_id=meeting_id,
                    grantee_type=grantee_type,
                    grantee_id=grantee_id,
                    granted_by=requester,
                )
            )
    return meeting_id


def seed_segments(
    engine: Engine,
    org_id: uuid.UUID,
    meeting_id: uuid.UUID,
    lines: list[str] | list[tuple[str, str]],
) -> list[str]:
    """Stored transcript segments of a meeting, shaped as ingest stores them (raw, canonical,
    script, folded search text) and marked `stored`; `lines` are texts or (speaker, text).
    Returns the segment ids in order."""
    from sqlalchemy import func, update

    from andai_core.db.models import Meeting, TranscriptSegment
    from andai_core.db.session import transaction
    from andai_core.text import canonical, fold, script_of

    ids = []
    with transaction(engine, org_id=org_id) as tx:
        tx.execute(
            update(Meeting)
            .where(Meeting.id == meeting_id)
            .values(transcript_state="stored", transcript_at=func.now())
        )
        for seq, line in enumerate(lines):
            speaker, raw = line if isinstance(line, tuple) else ("Asha", line)
            segment_id = f"seg_{meeting_id.hex[:12]}_{seq:04d}"
            roman = canonical(raw)
            tx.add(
                TranscriptSegment(
                    meeting_id=meeting_id,
                    id=segment_id,
                    org_id=org_id,
                    seq=seq,
                    start_s=seq * 5.0,
                    end_s=seq * 5.0 + 4.0,
                    speaker=speaker,
                    text_raw=raw,
                    text_canonical=roman,
                    script=script_of(raw),
                    language=None,
                    search_text=fold(roman),
                )
            )
            ids.append(segment_id)
    return ids


def mom_body(evidence: list[str], **overrides) -> dict:
    """A SPEC `MomDocument` citing the segment ids in `evidence` (three are used: a decision, two
    action items, a topic), without `meetingId` and `version`, which the store sets."""
    from datetime import UTC, datetime

    doc = {
        "templateId": "standard",
        "outputLanguage": "hinglish",
        "summary": "Budget review kal tak hoga.",
        "decisions": [{"text": "Deadline kal hai", "evidence": evidence[:1]}],
        "actionItems": [
            {
                "id": str(uuid.uuid4()),
                "text": "Ravi budget sheet bhejega",
                "owner": "Ravi",
                "due": "2026-11-01",
                "evidence": evidence[1:2] or evidence[:1],
            },
            {
                "id": str(uuid.uuid4()),
                "text": "Vendor se baat karni hai",
                "owner": "unassigned",
                "evidence": evidence[2:3] or evidence[:1],
            },
        ],
        "topics": [{"title": "Budget", "evidence": evidence[2:3]}],
        "risks": [{"text": "Vendor delay", "evidence": []}],
        "llmProfileId": str(uuid.uuid4()),
        "model": "test-model",
        "generatedAt": datetime(2026, 10, 31, 10, 0, tzinfo=UTC).isoformat(),
    }
    doc.update(overrides)
    return doc


@dataclass
class AccessOrg:
    """An org with the eight in-org callers, the meeting's `team` (requester, lead, team member)
    and `shared_team` (team grantee); `other_org` is the owner of a second org."""

    harness: Harness
    engine: Engine
    people: dict[str, Member]
    team: uuid.UUID
    shared_team: uuid.UUID
    clients: dict
    meeting_id: uuid.UUID | None = None  # the module's shared meeting, when a test module seeds one

    @property
    def org_id(self) -> uuid.UUID:
        return self.people["owner"].org_id

    def client(self, caller: str):
        """A signed-in client of `caller`, kept for the module."""
        if caller not in self.clients:
            self.clients[caller] = sign_in(self.harness, self.people[caller])
        return self.clients[caller]

    def actor(self, caller: str):
        from andai_core.meetings.access import Actor

        person = self.people[caller]
        role = {"owner": "owner", "admin": "admin", "other_org": "owner"}.get(caller, "member")
        return Actor(person.org_id, person.user_id, role)

    def meeting(self, *, status: str = "done", shares: bool = True, **fields) -> uuid.UUID:
        """A meeting the requester sent for `team`, shared with the user grantee and
        `shared_team` unless `shares` is false."""
        return seed_meeting(
            self.engine,
            self.org_id,
            self.people["requester"].user_id,
            self.team,
            status=status,
            shares=(
                (("user", self.people["user_grantee"].user_id), ("team", self.shared_team))
                if shares
                else ()
            ),
            **fields,
        )


def access_org(engine: Engine) -> AccessOrg:
    owner = new_member(engine)
    people = {"owner": owner, "admin": new_member(engine, role="admin", org_id=owner.org_id)}
    for caller in ("requester", "lead", "team_member", "user_grantee", "team_grantee", "unrelated"):
        people[caller] = new_member(engine, role="member", org_id=owner.org_id)
    people["other_org"] = new_member(engine)
    team = new_team(
        engine,
        owner.org_id,
        {
            people["requester"].user_id: "member",
            people["lead"].user_id: "lead",
            people["team_member"].user_id: "member",
        },
    )
    shared_team = new_team(engine, owner.org_id, {people["team_grantee"].user_id: "member"})
    return AccessOrg(make_harness(engine), engine, people, team, shared_team, {})


def served(app) -> set[tuple[str, str]]:
    """Every (method, path template) `app` serves, included routers flattened (an included router
    is one entry in `app.routes`)."""
    from fastapi.routing import iter_route_contexts

    return {
        (method, context.path)
        for context in iter_route_contexts(app.routes)
        for method in context.methods or ()
    }


@dataclass(frozen=True)
class AdminApiServer:
    url: str
    fake: FakeAdminApi


@pytest.fixture
def admin_api_server() -> Iterator[AdminApiServer]:
    """`FakeAdminApi` over real HTTP on a loopback port, for a CLI run in a subprocess."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx

    fake = FakeAdminApi()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            request = httpx.Request(
                self.command,
                f"http://admin-api.test{self.path}",
                headers=dict(self.headers.items()),
                content=self.rfile.read(length) if length else b"",
            )
            response = fake.handler(request)
            self.send_response(response.status_code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = do_POST = do_PATCH = do_DELETE = _serve

        def log_message(self, *args: object) -> None:  # keep test output quiet
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield AdminApiServer(f"http://127.0.0.1:{server.server_address[1]}", fake)
    finally:
        server.shutdown()
        server.server_close()
