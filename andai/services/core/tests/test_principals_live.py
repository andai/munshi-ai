"""The org's platform principal against the running stack's real admin-api (marked `live`).

Reads the stack's env file named by `ENV_FILE` (skips without it): `ADMIN_TOKEN` and the admin-api
host port (`ADMIN_API_PORT`, default 18057), the andai database on the published Postgres port
(`POSTGRES_HOST_PORT`, default 5458) as the app role, and `ANDAI_KEK`. It creates one org through
`create_org`, proves the stored key is a working `bot,tx` key of that org's platform user and that
the platform user carries the org's settings (bot-context reports the storage prefix `org/<org_id>`
and `capture_signal: false`; `INTERNAL_API_SECRET` from the env file), and removes what it created
afterwards (its tokens through the admin-api; its rows as the stack's
database superuser, so the dev database stays empty for a later bootstrap).

    ENV_FILE=$PWD/.work/.local/env.smoke uv run --directory andai pytest -q -m live \\
        services/core/tests/test_principals_live.py
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from andai_core.crypto import Keyring
from andai_core.db.session import create_app_engine, transaction
from andai_core.org.service import create_org
from andai_core.platform import principals
from andai_core.platform.admin_client import ADMIN_KEY_HEADER, HttpPlatformAdmin

pytestmark = pytest.mark.live


def _env() -> dict[str, str]:
    name = os.environ.get("ENV_FILE")
    if not name:
        pytest.skip("live test: set ENV_FILE to the running stack's env file")
    values: dict[str, str] = {}
    for line in Path(name).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    required = ("ADMIN_TOKEN", "ANDAI_DB_PASSWORD_APP", "ANDAI_KEK", "INTERNAL_API_SECRET")
    missing = [k for k in required if not values.get(k)]
    if missing:
        pytest.fail(f"{name} lacks {', '.join(missing)}", pytrace=False)
    return values


@pytest.fixture(scope="module")
def stack() -> dict[str, str]:
    return _env()


@pytest.fixture(scope="module")
def admin_url(stack: dict[str, str]) -> str:
    return f"http://127.0.0.1:{stack.get('ADMIN_API_PORT') or 18057}"


@pytest.fixture(scope="module")
def engines(stack: dict[str, str]) -> Iterator[tuple[Engine, Engine]]:
    port = stack.get("POSTGRES_HOST_PORT") or 5458
    app = create_app_engine(
        f"postgresql+psycopg://andai_app:{stack['ANDAI_DB_PASSWORD_APP']}@127.0.0.1:{port}/andai"
    )
    su_user = stack.get("DB_USER") or "postgres"
    superuser = create_engine(
        f"postgresql+psycopg://{su_user}:{stack.get('DB_PASSWORD', '')}@127.0.0.1:{port}/andai"
    )
    yield app, superuser
    app.dispose()
    superuser.dispose()


def _cleanup(superuser: Engine, org_id: uuid.UUID, owner_email: str) -> None:
    with superuser.begin() as conn:
        for table in ("audit_events", "platform_principals", "org_memberships"):
            conn.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id})
        conn.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org_id})
        conn.execute(text("DELETE FROM users WHERE email = :e"), {"e": owner_email})


def test_create_org_provisions_a_working_principal_through_the_real_admin_api(
    stack: dict[str, str], admin_url: str, engines: tuple[Engine, Engine]
) -> None:
    app, superuser = engines
    admin_key = stack["ADMIN_TOKEN"]
    admin = HttpPlatformAdmin(admin_url, SecretStr(admin_key))
    keyring = Keyring(base64.b64decode(stack["ANDAI_KEK"]))
    owner_email = f"live-{uuid.uuid4().hex[:12]}@example.test"
    headers = {ADMIN_KEY_HEADER: admin_key}
    created = None
    extra = None
    try:
        created = create_org(
            app,
            name=f"Live principal {uuid.uuid4().hex[:8]}",
            owner_email=owner_email,
            owner_display_name=None,
            admin=admin,
            keyring=keyring,
            now=datetime.now(UTC),
        )
        org_id = created.org_id

        with transaction(app, org_id=org_id) as tx:
            key = principals.open_key(tx, keyring, org_id)
            rows = tx.execute(
                text("SELECT platform_user_id FROM platform_principals")
            ).scalars().all()
        assert rows == [created.platform_user_id]
        assert key is not None

        with httpx.Client(base_url=admin_url, timeout=10) as client:
            user = client.get(f"/admin/users/{created.platform_user_id}", headers=headers)
            assert user.status_code == 200
            assert user.json()["email"] == principals.principal_email(org_id)
            tokens = client.get(f"/admin/users/{created.platform_user_id}/tokens", headers=headers)
            assert tokens.status_code == 200
            assert [sorted(t["scopes"]) for t in tokens.json()] == [["bot", "tx"]]
            assert all(key not in str(v) for t in tokens.json() for v in t.values())
            # The stored key is a working key of that platform user.
            own = client.get("/user/webhook", headers={"X-API-Key": key})
            assert own.status_code == 200, own.status_code

        # The principal carries the org's platform settings, as meeting-api reads them at spawn.
        with httpx.Client(base_url=admin_url, timeout=10) as client:
            user = client.get(f"/admin/users/{created.platform_user_id}", headers=headers).json()
            context = client.get(
                f"/internal/users/{created.platform_user_id}/bot-context",
                headers={"X-Internal-Secret": stack["INTERNAL_API_SECRET"]},
            )
        assert user["max_concurrent_bots"] == principals.DEFAULT_MAX_CONCURRENT_BOTS
        assert context.status_code == 200, context.status_code
        assert context.json()["storage_prefix"] == f"org/{org_id}"
        assert context.json()["capture_signal"] is False
        assert context.json()["max_concurrent"] == principals.DEFAULT_MAX_CONCURRENT_BOTS

        # Provisioning the same org again finds the same platform user.
        extra = principals.provision(admin, org_id)
        assert extra.platform_user_id == created.platform_user_id

        with superuser.connect() as conn:
            stored = conn.execute(
                text("SELECT key_ciphertext FROM platform_principals WHERE org_id = :o"),
                {"o": org_id},
            ).scalar_one()
        assert key not in stored and admin_key not in stored
    finally:
        if extra is not None:
            principals.discard(admin, extra)
        if created is not None:
            with httpx.Client(base_url=admin_url, timeout=10) as client:
                listed = client.get(
                    f"/admin/users/{created.platform_user_id}/tokens", headers=headers
                ).json()
                for token in listed:
                    client.delete(f"/admin/tokens/{token['id']}", headers=headers)
            _cleanup(superuser, created.org_id, owner_email)
