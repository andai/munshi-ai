"""The operator's erasure across orgs: `python -m andai_core erase-user` and `erase-status`."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import Engine, text

from andai_core.__main__ import main
from andai_core.db.models import OrgMembership
from andai_core.db.session import transaction
from andai_core.erasure import runner
from andai_core.erasure.requests import remove_from_org
from andai_core.meetings.service import Meetings
from conftest import make_harness, new_member, sign_in


def _cli(engine: Engine, *args: str) -> int:
    return main(list(args), engine=engine)


def _requests(superuser_engine: Engine, subject: uuid.UUID) -> list[dict]:
    with superuser_engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT id, org_id, audit_org_id, source, account, requested_by, step"
                    " FROM erasure_requests WHERE subject_user_id = :s ORDER BY account, created_at"
                ),
                {"s": subject},
            ).mappings()
        ]


def _join(engine: Engine, org_id: uuid.UUID, user_id: uuid.UUID, role: str = "member") -> None:
    with transaction(engine, org_id=org_id) as tx:
        tx.add(OrgMembership(org_id=org_id, user_id=user_id, role=role))


def test_a_user_in_two_orgs_gets_one_request_per_org(
    app_engine: Engine, superuser_engine: Engine, capsys: pytest.CaptureFixture
) -> None:
    person = new_member(app_engine, role="member", org_id=new_member(app_engine).org_id)
    second = new_member(app_engine)
    _join(app_engine, second.org_id, person.user_id, role="admin")

    code = _cli(
        app_engine, "erase-user", "--email", person.email, "--confirm", person.email.upper()
    )

    out = capsys.readouterr()
    assert code == 0, out.err
    rows = _requests(superuser_engine, person.user_id)
    assert {r["org_id"] for r in rows} == {person.org_id, second.org_id}
    assert all(r["org_id"] == r["audit_org_id"] for r in rows)
    assert [r["account"] for r in rows] == [False, True]
    assert {(r["source"], r["requested_by"], r["step"]) for r in rows} == {
        ("operator", None, "stop_live")
    }
    with superuser_engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM org_memberships WHERE user_id = :u"), {"u": person.user_id}
        ).scalar_one() == 0
        audited = conn.execute(
            text("SELECT org_id FROM audit_events WHERE action = 'user.erase.request'"
                 " AND target = :t"), {"t": f"user:{person.user_id}"}
        ).scalars().all()
    assert sorted(map(str, audited)) == sorted([str(person.org_id), str(second.org_id)])
    # Ids and counts only: never the address.
    assert person.email not in out.out and person.email not in out.err
    assert "2 requests created; 2 memberships removed" in out.out
    for r in rows:
        assert str(r["id"]) in out.out

    status = _cli(app_engine, "erase-status", "--id", str(rows[1]["id"]))
    shown = capsys.readouterr().out
    assert status == 0
    assert "state pending, step stop_live" in shown and "account yes" in shown
    assert person.email not in shown


def test_an_owner_is_refused_and_there_is_no_override(
    app_engine: Engine, superuser_engine: Engine, capsys: pytest.CaptureFixture
) -> None:
    owner = new_member(app_engine)
    _join(app_engine, new_member(app_engine).org_id, owner.user_id)

    code = _cli(app_engine, "erase-user", "--email", owner.email, "--confirm", owner.email)

    assert code == 2
    assert "owner_cannot_be_erased" in capsys.readouterr().err
    assert _requests(superuser_engine, owner.user_id) == []
    for flag in ("--force", "--override", "--allow-owner"):
        with pytest.raises(SystemExit) as exc:
            _cli(app_engine, "erase-user", "--email", owner.email, "--confirm", owner.email, flag)
        assert exc.value.code == 2
    with superuser_engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM org_memberships WHERE user_id = :u"), {"u": owner.user_id}
        ).scalar_one() == 2


def test_the_confirmation_must_repeat_the_address(
    app_engine: Engine, superuser_engine: Engine, capsys: pytest.CaptureFixture
) -> None:
    person = new_member(app_engine, role="member", org_id=new_member(app_engine).org_id)

    assert _cli(app_engine, "erase-user", "--email", person.email,
                "--confirm", "someone-else@example.test") == 2
    assert _cli(app_engine, "erase-user", "--email", "nobody-here@example.test",
                "--confirm", "nobody-here@example.test") == 1
    capsys.readouterr()
    assert _requests(superuser_engine, person.user_id) == []


def test_a_user_with_no_membership_needs_an_org_they_belonged_to(
    app_engine: Engine, superuser_engine: Engine, capsys: pytest.CaptureFixture
) -> None:
    harness = make_harness(app_engine)
    person = new_member(app_engine, role="member", org_id=new_member(app_engine).org_id)
    sign_in(harness, person)  # login.success names them in their org's trail
    with transaction(app_engine, org_id=person.org_id) as tx:
        remove_from_org(tx, person.org_id, person.user_id)  # they left the org
    stranger_org = new_member(app_engine).org_id
    args = ("erase-user", "--email", person.email, "--confirm", person.email)

    assert _cli(app_engine, *args) == 2
    assert "--audit-org" in capsys.readouterr().err
    assert _cli(app_engine, *args, "--audit-org", str(stranger_org)) == 2
    assert "never belonged" in capsys.readouterr().err
    assert _cli(app_engine, *args, "--audit-org", str(uuid.uuid4())) == 2
    assert _requests(superuser_engine, person.user_id) == []

    assert _cli(app_engine, *args, "--audit-org", str(person.org_id)) == 0
    capsys.readouterr()
    [request] = _requests(superuser_engine, person.user_id)
    assert (request["org_id"], request["audit_org_id"], request["account"], request["step"]) == (
        None, person.org_id, True, "account"
    )
    # A second one while it is open is refused.
    assert _cli(app_engine, *args, "--audit-org", str(person.org_id)) == 1
    assert "erasure_pending" in capsys.readouterr().err

    s = harness.services
    meetings = Meetings(app_engine, s.keyring, s.gateway, s.settings, s.clock)
    state = asyncio.run(
        runner.run(app_engine, meetings, person.org_id, request["id"], clock=s.clock)
    )
    assert state == "done"
    with superuser_engine.connect() as conn:
        assert conn.execute(
            text("SELECT status FROM users WHERE id = :u"), {"u": person.user_id}
        ).scalar_one() == "erased"
        completed = conn.execute(
            text("SELECT org_id FROM audit_events WHERE action = 'user.erase.complete'"
                 " AND target = :t"), {"t": f"user:{person.user_id}"}
        ).scalars().all()
    assert completed == [person.org_id]
    # The address is gone: the account can no longer be found by it.
    assert _cli(app_engine, *args, "--audit-org", str(person.org_id)) == 1
    capsys.readouterr()


def test_erase_status_of_an_unknown_request(app_engine: Engine, capsys) -> None:
    assert _cli(app_engine, "erase-status", "--id", str(uuid.uuid4())) == 1
    assert _cli(app_engine, "erase-status", "--id", "not-an-id") == 2
    capsys.readouterr()
