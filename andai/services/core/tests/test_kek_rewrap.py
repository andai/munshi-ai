"""Key rotation against Postgres: `sealed.rewrap_all` and `python -m andai_core kek-rewrap |
kek-status` move every sealed value onto the active KEK, idempotently, crash-safely and without
overwriting a value re-sealed meanwhile; TOTP and the org platform keys keep working throughout.

Each test uses a brand-new database (`fresh_app_engine`) seeded with synthetic secrets only: three
orgs with a platform principal and five users with a TOTP enrolment, all sealed under key A. Keys
are random per run.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from sqlalchemy import Engine, text

from andai_core import sealed
from andai_core.__main__ import main
from andai_core.auth import mfa
from andai_core.crypto import KEY_BYTES, Keyring, KeyUnavailable, Sealed
from andai_core.db.session import transaction
from andai_core.platform import principals
from conftest import give_principal, make_harness, new_member

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@dataclass
class World:
    old: bytes
    new: bytes
    org_keys: dict[uuid.UUID, str]
    totps: dict[uuid.UUID, pyotp.TOTP]

    @property
    def rotating(self) -> Keyring:
        """New key active, old key retired: the keyring a rotation runs with."""
        return Keyring(self.new, [self.old])

    @property
    def old_id(self) -> str:
        return Keyring(self.old).key_id

    @property
    def new_id(self) -> str:
        return Keyring(self.new).key_id


def _world(engine: Engine) -> World:
    old, new = os.urandom(KEY_BYTES), os.urandom(KEY_BYTES)
    harness = make_harness(engine)
    harness.services.keyring = Keyring(old)
    org_keys: dict[uuid.UUID, str] = {}
    totps: dict[uuid.UUID, pyotp.TOTP] = {}
    members = [new_member(engine) for _ in range(3)]
    for member in members:
        org_keys[member.org_id] = give_principal(harness, engine, member.org_id)
    members += [new_member(engine, org_id=members[0].org_id) for _ in range(2)]
    for member in members:
        with transaction(engine) as tx:
            started = mfa.start(
                tx, Keyring(old), user_id=member.user_id, email=member.email, now=T0
            )
            totp = pyotp.parse_uri(started.otpauth_uri)
            mfa.confirm(tx, Keyring(old), user_id=member.user_id, code=totp.at(T0), now=T0)
        totps[member.user_id] = totp
    return World(old=old, new=new, org_keys=org_keys, totps=totps)


def _secrets_work(engine: Engine, world: World, keyring: Keyring, at: datetime) -> None:
    """Every TOTP enrolment accepts its current code and every org's platform key opens."""
    for user_id, totp in world.totps.items():
        with transaction(engine) as tx:
            assert mfa.check(tx, keyring, user_id=user_id, code=totp.at(at), now=at) == mfa.TOTP
    for org_id, key in world.org_keys.items():
        with transaction(engine, org_id=org_id) as tx:
            assert principals.open_key(tx, keyring, org_id) == key


def _totals(engine: Engine) -> dict[str, int]:
    totals: dict[str, int] = {}
    for (_, key_id), n in sealed.counts_by_key(engine).items():
        totals[key_id] = totals.get(key_id, 0) + n
    return totals


def _ciphertexts(superuser_engine: Engine, db_engine: Engine) -> set[tuple[str, str]]:
    """Every sealed (ciphertext, key_id) in the test database, read past RLS."""
    url = superuser_engine.url.set(database=db_engine.url.database)
    from sqlalchemy import create_engine

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT secret_ciphertext, key_id FROM user_totp"
                    " UNION ALL SELECT key_ciphertext, key_id FROM platform_principals"
                )
            ).all()
    finally:
        engine.dispose()
    return {(ct, k) for ct, k in rows}


def test_rewrap_moves_every_value_to_the_active_key_and_secrets_keep_working(
    fresh_app_engine: Engine,
) -> None:
    engine = fresh_app_engine
    world = _world(engine)
    assert _totals(engine) == {world.old_id: 8}

    result = sealed.rewrap_all(engine, world.rotating)

    assert {t: (c.rewrapped, c.skipped, c.failed) for t, c in result.items()} == {
        "user_totp": (5, 0, 0),
        "platform_principals": (3, 0, 0),
        "export_requests": (0, 0, 0),
    }
    assert _totals(engine) == {world.new_id: 8}  # nothing left under the old key
    assert sealed.counts_by_key(engine) == {
        ("user_totp", world.new_id): 5,
        ("platform_principals", world.new_id): 3,
    }
    # the old key can go: the new key alone opens everything
    _secrets_work(engine, world, Keyring(world.new), T0 + timedelta(minutes=1))


def test_a_second_run_finds_nothing_to_do(fresh_app_engine: Engine) -> None:
    engine = fresh_app_engine
    world = _world(engine)
    sealed.rewrap_all(engine, world.rotating)

    again = sealed.rewrap_all(engine, world.rotating)

    assert all((c.rewrapped, c.skipped, c.failed) == (0, 0, 0) for c in again.values())


class _CrashingKeyring(Keyring):
    """Re-wraps normally, then raises on call number `crash_at` (a crash mid-run)."""

    def __init__(self, active: bytes, retired: list[bytes], crash_at: int) -> None:
        super().__init__(active, retired)
        self.calls = 0
        self.crash_at = crash_at

    def rewrap(self, ciphertext: str, key_id: str) -> Sealed:
        self.calls += 1
        if self.calls == self.crash_at:
            raise RuntimeError("injected crash")
        return super().rewrap(ciphertext, key_id)


def test_a_run_that_crashes_after_its_first_batch_is_finished_by_a_second_run(
    fresh_app_engine: Engine,
) -> None:
    engine = fresh_app_engine
    world = _world(engine)

    with pytest.raises(RuntimeError, match="injected crash"):
        sealed.rewrap_all(engine, _CrashingKeyring(world.new, [world.old], crash_at=3), batch=2)

    # the first batch of two committed; the batch that crashed rolled back
    assert _totals(engine) == {world.old_id: 6, world.new_id: 2}
    _secrets_work(engine, world, world.rotating, T0 + timedelta(minutes=1))

    resumed = sealed.rewrap_all(engine, world.rotating, batch=2)

    assert {t: (c.rewrapped, c.skipped, c.failed) for t, c in resumed.items()} == {
        "user_totp": (3, 0, 0),  # no double work: the two committed rows are not touched again
        "platform_principals": (3, 0, 0),
        "export_requests": (0, 0, 0),
    }
    assert _totals(engine) == {world.new_id: 8}
    _secrets_work(engine, world, Keyring(world.new), T0 + timedelta(minutes=2))


class _RacingKeyring(Keyring):
    """On its first re-wrap, another transaction re-seals that same TOTP row under the active key
    (a re-enrolment between the job's read and its write)."""

    def __init__(self, active: bytes, retired: list[bytes], engine: Engine) -> None:
        super().__init__(active, retired)
        self.engine = engine
        self.raced: tuple[str, str] | None = None

    def rewrap(self, ciphertext: str, key_id: str) -> Sealed:
        if self.raced is None:
            with transaction(self.engine) as tx:
                user_id = tx.scalar(
                    text("SELECT user_id FROM user_totp WHERE secret_ciphertext = :ct"),
                    {"ct": ciphertext},
                )
                if user_id is not None:
                    fresh = self.seal("RACEDSECRETAAAAA", f"user_totp:{user_id}")
                    tx.execute(
                        text(
                            "UPDATE user_totp SET secret_ciphertext = :ct, key_id = :k"
                            " WHERE user_id = :u"
                        ),
                        {"ct": fresh.ciphertext, "k": fresh.key_id, "u": user_id},
                    )
                    self.raced = (str(user_id), fresh.ciphertext)
        return super().rewrap(ciphertext, key_id)


def test_a_value_re_sealed_between_read_and_write_is_not_overwritten(
    fresh_app_engine: Engine,
) -> None:
    engine = fresh_app_engine
    world = _world(engine)
    keyring = _RacingKeyring(world.new, [world.old], engine)

    result = sealed.rewrap_all(engine, keyring)

    assert keyring.raced is not None
    assert (result["user_totp"].rewrapped, result["user_totp"].skipped) == (4, 1)
    user_id, raced_ct = keyring.raced
    with transaction(engine) as tx:
        stored = tx.execute(
            text("SELECT secret_ciphertext, key_id FROM user_totp WHERE user_id = :u"),
            {"u": user_id},
        ).one()
    assert tuple(stored) == (raced_ct, world.new_id)  # the concurrent write survived
    assert Keyring(world.new).open(stored[0], stored[1], f"user_totp:{user_id}") == (
        b"RACEDSECRETAAAAA"
    )
    assert _totals(engine) == {world.new_id: 8}


def test_a_dry_run_writes_nothing(fresh_app_engine: Engine, superuser_engine: Engine) -> None:
    engine = fresh_app_engine
    world = _world(engine)
    before = _ciphertexts(superuser_engine, engine)

    result = sealed.rewrap_all(engine, world.rotating, dry_run=True)

    assert {t: c.rewrapped for t, c in result.items()} == {
        "user_totp": 5, "platform_principals": 3, "export_requests": 0,
    }
    assert _ciphertexts(superuser_engine, engine) == before
    assert _totals(engine) == {world.old_id: 8}


def test_a_value_under_a_key_that_is_not_loaded_fails_and_the_run_still_ends(
    fresh_app_engine: Engine,
) -> None:
    engine = fresh_app_engine
    world = _world(engine)
    stranger = os.urandom(KEY_BYTES)
    member = new_member(engine)
    with transaction(engine) as tx:
        mfa.start(tx, Keyring(stranger), user_id=member.user_id, email=member.email, now=T0)

    result = sealed.rewrap_all(engine, world.rotating, batch=2)

    assert (result["user_totp"].rewrapped, result["user_totp"].failed) == (5, 1)
    assert _totals(engine) == {world.new_id: 8, Keyring(stranger).key_id: 1}


def test_rewrap_refuses_an_ephemeral_active_key(fresh_app_engine: Engine) -> None:
    with pytest.raises(KeyUnavailable):
        sealed.rewrap_all(fresh_app_engine, Keyring(os.urandom(KEY_BYTES), ephemeral=True))


# ─── the CLI ─────────────────────────────────────────────────────────────────────────────────────

_LONG_BASE64 = re.compile(r"[A-Za-z0-9+/_=-]{41,}")


def _cli(capsys, engine: Engine, keyring: Keyring, *args: str) -> tuple[int, str]:
    code = main(list(args), engine=engine, keyring=keyring)
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert not _LONG_BASE64.search(output), "the CLI printed something that looks like a value"
    return code, output


def test_the_cli_reports_counts_and_key_ids_only(fresh_app_engine: Engine, capsys) -> None:
    engine = fresh_app_engine
    world = _world(engine)

    status_before, before = _cli(capsys, engine, world.rotating, "kek-status")
    dry_code, dry = _cli(capsys, engine, world.rotating, "kek-rewrap", "--dry-run")
    after_dry, _ = _cli(capsys, engine, world.rotating, "kek-status")
    rewrap_code, rewrap = _cli(capsys, engine, world.rotating, "kek-rewrap", "--batch", "2")
    status_after, after = _cli(capsys, engine, world.rotating, "kek-status")

    assert (status_before, dry_code, after_dry, rewrap_code, status_after) == (1, 0, 1, 0, 0)
    assert f"user_totp: {world.old_id} (retired) 5" in before
    assert f"platform_principals: {world.old_id} (retired) 3" in before
    assert "8 values are under a key other than the active key" in before
    assert "user_totp: would re-wrap 5, skipped 0, failed 0" in dry
    assert "nothing written" in dry
    assert "platform_principals: re-wrapped 3, skipped 0, failed 0" in rewrap
    assert f"user_totp: {world.new_id} (active) 5" in after
    assert f"user_totp: {world.old_id} (retired) 0" in after
    assert "0 values are under a key other than the active key" in after
    for secret in world.org_keys.values():
        assert secret not in before + dry + rewrap + after


def test_the_cli_refuses_without_a_kek(fresh_app_engine: Engine, capsys) -> None:
    ephemeral = Keyring(os.urandom(KEY_BYTES), ephemeral=True)

    for command in ("kek-rewrap", "kek-status"):
        assert main([command], engine=fresh_app_engine, keyring=ephemeral) == 2
        assert "ANDAI_KEK" in capsys.readouterr().err


def test_the_cli_exits_1_when_a_value_cannot_be_re_wrapped(
    fresh_app_engine: Engine, capsys
) -> None:
    engine = fresh_app_engine
    world = _world(engine)
    member = new_member(engine)
    with transaction(engine) as tx:
        mfa.start(
            tx, Keyring(os.urandom(KEY_BYTES)), user_id=member.user_id, email=member.email, now=T0
        )

    code, output = _cli(capsys, engine, world.rotating, "kek-rewrap")

    assert code == 1
    assert "user_totp: re-wrapped 5, skipped 0, failed 1" in output
    status, listing = _cli(capsys, engine, world.rotating, "kek-status")
    assert status == 1 and "(not loaded) 1" in listing


def test_export_bundles_are_rewrapped_a_few_at_a_time_and_still_open(
    fresh_app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from andai_core.db.models import ExportRequest
    from andai_core.dpdp import builder

    engine = fresh_app_engine
    world = _world(engine)
    org_id = next(iter(world.org_keys))
    subject = new_member(engine, role="member", org_id=org_id)
    bundles: dict[uuid.UUID, bytes] = {}
    for n in range(7):
        request_id = uuid.uuid4()
        part = builder.Built(files={"manifest.json": f'{{"n": {n}}}'.encode()})
        sealed_part = builder.seal_part(Keyring(world.old), org_id, request_id, part)
        with transaction(engine, org_id=org_id) as tx:
            tx.add(
                ExportRequest(
                    id=request_id, org_id=org_id, subject_user_id=subject.user_id,
                    requested_by=subject.user_id, source="self",
                    state="ready" if n else "expired",
                    bundle_ciphertext=sealed_part.ciphertext if n else None,
                    key_id=sealed_part.key_id if n else None,
                    expires_at=T0 + timedelta(days=7),
                )
            )
        if n:
            bundles[request_id] = part.files["manifest.json"]
    assert sealed.counts_by_key(engine)[("export_requests", world.old_id)] == 6  # not the expired

    batches: dict[str, set[int]] = {}
    original = sealed._rewrap_scope

    def recording(engine_, keyring, column, org, count, *, batch, dry_run):
        batches.setdefault(column.table, set()).add(batch)
        return original(engine_, keyring, column, org, count, batch=batch, dry_run=dry_run)

    monkeypatch.setattr(sealed, "_rewrap_scope", recording)
    result = sealed.rewrap_all(engine, world.rotating, batch=100)

    assert result["export_requests"].rewrapped == 6
    assert ("export_requests", world.old_id) not in sealed.counts_by_key(engine)
    for request_id, manifest in bundles.items():
        with transaction(engine, org_id=org_id) as tx:
            row = tx.get(ExportRequest, request_id)
            files = builder.open_part(
                Keyring(world.new), org_id, request_id, row.bundle_ciphertext, row.key_id
            )
        assert files == {"manifest.json": manifest}
    cap = next(c for c in sealed.SEALED_COLUMNS if c.table == "export_requests").batch_cap
    assert batches["export_requests"] == {cap} and cap < 100
    assert batches["user_totp"] == {100}
