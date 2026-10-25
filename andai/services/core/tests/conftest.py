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
