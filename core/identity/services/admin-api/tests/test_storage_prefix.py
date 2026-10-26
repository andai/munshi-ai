"""Per-user object-storage prefix (``users.data["storage_prefix"]``) and the per-user tape switch.

andai-core provisions one principal user per org and stamps it with ``storage_prefix =
"org/{org_id}"`` and ``diagnostics.capture_signal = false`` through the admin PATCH. meeting-api
reads both from ``/internal/users/{id}/bot-context`` at spawn. These tests pin:

  * the admin PATCH accepts exactly the ``org/<uuid>`` shape and refuses everything else with 422;
  * the PATCH merges ``diagnostics`` instead of replacing it, and ``null`` removes the prefix;
  * bot-context echoes a well-formed prefix, omits an absent one, and withholds a malformed value
    that reached the row by some other door;
  * no user-tier credential can set the prefix.
"""
import pytest
from sqlalchemy import create_engine, text

from admin_api.app.main import STORAGE_PREFIX_PATTERN, valid_storage_prefix

from conftest import requires_docker
from test_stack_admin_api import ADMIN_TOKEN, INTERNAL_SECRET, _admin, client  # noqa: F401

ORG_ID = "3f2b8c1e-9a4d-4e7f-8b21-5c6d7e8f9a0b"
PREFIX = f"org/{ORG_ID}"


def _internal():
    return {"X-Internal-Secret": INTERNAL_SECRET}


# ── the shape check: pure ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [PREFIX, "org/00000000-0000-0000-0000-000000000000"])
def test_valid_storage_prefix_accepts_org_uuid(value):
    assert valid_storage_prefix(value) == value


@pytest.mark.parametrize("value", [
    None, "", "org/", "org/x", "org/../recordings", "../org/" + ORG_ID, "/org/" + ORG_ID,
    PREFIX + "/", PREFIX + "/recordings", PREFIX.upper(), "ORG/" + ORG_ID,
    "org/" + "-" * 36, "org/" + ORG_ID.replace("-", ""), "tenant/" + ORG_ID,
    PREFIX + "\n", 42, {"prefix": PREFIX},
])
def test_valid_storage_prefix_rejects_everything_else(value):
    assert valid_storage_prefix(value) is None


def test_pattern_is_anchored():
    assert STORAGE_PREFIX_PATTERN.startswith("^") and STORAGE_PREFIX_PATTERN.endswith("$")


# ── the edge: admin PATCH and bot-context (testcontainers PG) ────────────────────────────────────

def _new_user(client, email="principal@example.com") -> int:
    r = client.post("/admin/users", headers=_admin(), json={"email": email})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@requires_docker
def test_admin_patch_sets_prefix_tape_switch_and_cap_and_bot_context_echoes_them(client):
    uid = _new_user(client)
    before = client.get(f"/internal/users/{uid}/bot-context", headers=_internal()).json()
    assert "storage_prefix" not in before
    assert before["capture_signal"] is True

    r = client.patch(f"/admin/users/{uid}", headers=_admin(), json={
        "max_concurrent_bots": 5,
        "data": {"storage_prefix": PREFIX, "diagnostics": {"capture_signal": False}},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_concurrent_bots"] == 5
    assert body["data"]["storage_prefix"] == PREFIX
    assert body["data"]["diagnostics"] == {"capture_signal": False}

    ctx = client.get(f"/internal/users/{uid}/bot-context", headers=_internal()).json()
    assert ctx["storage_prefix"] == PREFIX
    assert ctx["capture_signal"] is False
    assert ctx["max_concurrent"] == 5


@requires_docker
def test_admin_patch_is_idempotent_and_merges_diagnostics(client, pg_url):
    uid = _new_user(client, "merge@example.com")
    # A switch stored by another door must survive a PATCH that sets capture_signal.
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(
            "UPDATE users SET data = jsonb_set(coalesce(data::jsonb, '{}'::jsonb), '{diagnostics}', "
            "'{\"other_switch\": \"off\"}'::jsonb) WHERE id = :id"
        ), {"id": uid})
    eng.dispose()

    patch = {"data": {"storage_prefix": PREFIX, "diagnostics": {"capture_signal": False}}}
    first = client.patch(f"/admin/users/{uid}", headers=_admin(), json=patch)
    second = client.patch(f"/admin/users/{uid}", headers=_admin(), json=patch)
    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert second.json()["data"]["diagnostics"] == {"other_switch": "off", "capture_signal": False}


@requires_docker
@pytest.mark.parametrize("bad", [
    "org/x", "../org/" + ORG_ID, "org/../" + ORG_ID, "/org/" + ORG_ID, "/etc/passwd",
    PREFIX + "/recordings", PREFIX.upper(), "", "recordings", "org/" + "-" * 36,
])
def test_admin_patch_rejects_malformed_prefix_with_422(client, bad):
    uid = _new_user(client, "bad-prefix@example.com")
    r = client.patch(f"/admin/users/{uid}", headers=_admin(),
                     json={"data": {"storage_prefix": bad}})
    assert r.status_code == 422, r.text
    ctx = client.get(f"/internal/users/{uid}/bot-context", headers=_internal()).json()
    assert "storage_prefix" not in ctx


@requires_docker
def test_admin_patch_rejects_unknown_diagnostics_keys(client):
    uid = _new_user(client, "diag@example.com")
    r = client.patch(f"/admin/users/{uid}", headers=_admin(),
                     json={"data": {"diagnostics": {"capture_everything": True}}})
    assert r.status_code == 422, r.text


@requires_docker
def test_null_removes_the_prefix(client):
    uid = _new_user(client, "clear@example.com")
    client.patch(f"/admin/users/{uid}", headers=_admin(), json={"data": {"storage_prefix": PREFIX}})
    r = client.patch(f"/admin/users/{uid}", headers=_admin(), json={"data": {"storage_prefix": None}})
    assert r.status_code == 200, r.text
    ctx = client.get(f"/internal/users/{uid}/bot-context", headers=_internal()).json()
    assert "storage_prefix" not in ctx


@requires_docker
def test_bot_context_withholds_a_malformed_stored_prefix(client, pg_url):
    uid = _new_user(client, "stored-junk@example.com")
    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(text(
            "UPDATE users SET data = jsonb_set(coalesce(data::jsonb, '{}'::jsonb), "
            "'{storage_prefix}', '\"../org/x\"'::jsonb) WHERE id = :id"
        ), {"id": uid})
    eng.dispose()
    ctx = client.get(f"/internal/users/{uid}/bot-context", headers=_internal()).json()
    assert "storage_prefix" not in ctx


@requires_docker
def test_prefix_cannot_be_set_without_the_admin_token(client):
    uid = _new_user(client, "no-admin@example.com")
    token = client.post(f"/admin/users/{uid}/tokens", headers=_admin(),
                        params={"scopes": "bot,tx"}).json()["token"]
    for headers in ({}, {"X-API-Key": token}, {"X-Admin-API-Key": "wrong"}):
        r = client.patch(f"/admin/users/{uid}", headers=headers,
                         json={"data": {"storage_prefix": PREFIX}})
        assert r.status_code in (401, 403), (headers, r.status_code)
    # A user-tier writer of users.data takes its own fields only.
    r = client.put("/user/webhook", headers={"X-API-Key": token},
                   json={"webhook_url": "https://hooks.example.com/x", "storage_prefix": PREFIX})
    ctx = client.get(f"/internal/users/{uid}/bot-context", headers=_internal()).json()
    assert "storage_prefix" not in ctx, r.text
