"""Sender resolution and quarantine attribution (D-58) through the 0013 functions, on a disposable
Postgres as the app role."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text

import mailkit
from andai_core.db.models import Org, OrgMembership, Team, TeamMembership, User
from andai_core.db.session import transaction
from andai_intake import sender as senders
from andai_intake.auth.verdict import Policy, verify


def _domain() -> str:
    return f"d{uuid.uuid4().hex[:12]}.example.test"


def _org(engine: Engine, *domains: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    with transaction(engine, org_id=org_id) as tx:
        tx.add(
            Org(id=org_id, name=f"Org {org_id.hex[:8]}", slug=f"org-{org_id.hex}",
                allowed_email_domains=list(domains))
        )
    return org_id


def _user(engine: Engine, email: str, status: str = "active") -> uuid.UUID:
    user_id = uuid.uuid4()
    with transaction(engine) as tx:
        tx.add(User(id=user_id, email=email, display_name="Test", status=status))
    return user_id


def _join(engine: Engine, user_id: uuid.UUID, org_id: uuid.UUID, role: str = "member") -> None:
    with transaction(engine, org_id=org_id) as tx:
        tx.add(OrgMembership(org_id=org_id, user_id=user_id, role=role))


def _team(engine: Engine, org_id: uuid.UUID, *user_ids: uuid.UUID) -> uuid.UUID:
    team_id = uuid.uuid4()
    with transaction(engine, org_id=org_id) as tx:
        tx.add(Team(id=team_id, org_id=org_id, name=f"Team {team_id.hex[:8]}"))
        tx.flush()
        for user_id in user_ids:
            tx.add(TeamMembership(team_id=team_id, org_id=org_id, user_id=user_id, role="member"))
    return team_id


def _member(engine: Engine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    user_id = _user(engine, email)
    _join(engine, user_id, org_id)
    return user_id


def test_an_active_member_of_one_org_resolves_with_their_only_team(app_engine: Engine) -> None:
    domain = _domain()
    org = _org(app_engine, domain)
    ana = _member(app_engine, org, f"ana@{domain}")
    assert senders.resolve(app_engine, f"ana@{domain}") == senders.Resolution(ana, org, None, None)

    team = _team(app_engine, org, ana)
    resolved = senders.resolve(app_engine, f"ana@{domain}")
    assert (resolved.user_id, resolved.org_id, resolved.team_id) == (ana, org, team)
    assert resolved.resolved

    _team(app_engine, org, ana)  # a second team: no team
    assert senders.resolve(app_engine, f"ana@{domain}").team_id is None


def test_disabled_invited_or_membershipless_users_are_unknown(app_engine: Engine) -> None:
    domain = _domain()
    org = _org(app_engine)  # no allowed domains: nothing to attribute to
    _join(app_engine, _user(app_engine, f"off@{domain}", status="disabled"), org)
    _join(app_engine, _user(app_engine, f"new@{domain}", status="invited"), org)
    _user(app_engine, f"loose@{domain}")
    for local in ("off", "new", "loose", "nobody"):
        result = senders.resolve(app_engine, f"{local}@{domain}")
        assert result == senders.Resolution(None, None, None, "unknown_user"), local


def test_case_and_plus_addressing_fold_as_stored(app_engine: Engine) -> None:
    domain = _domain()
    org = _org(app_engine)
    ana = _member(app_engine, org, f"ana@{domain}")
    tagged = _member(app_engine, org, f"bo+work@{domain}")
    assert senders.resolve(app_engine, f"Ana@{domain.upper()}").user_id == ana
    assert senders.resolve(app_engine, f"ana+notes@{domain}").user_id == ana
    assert senders.resolve(app_engine, f"BO+work@{domain}").user_id == tagged
    assert senders.resolve(app_engine, f"bo+other@{domain}").reason == "unknown_user"
    assert senders.resolve(app_engine, f"bo@{domain}").reason == "unknown_user"
    assert senders.candidates("Ana+X@D.test") == ["ana+x@d.test", "ana@d.test"]
    assert senders.candidates("+x@d.test") == ["+x@d.test"]


def test_an_unknown_address_is_attributed_to_the_one_org_claiming_its_domain(
    app_engine: Engine,
) -> None:
    domain, shared = _domain(), _domain()
    org_b = _org(app_engine, domain.upper())  # stored in any case
    _org(app_engine, shared)
    _org(app_engine, shared)
    assert senders.resolve(app_engine, f"stranger@{domain}") == senders.Resolution(
        None, org_b, None, "unknown_user"
    )
    assert senders.resolve(app_engine, f"stranger@{shared}").org_id is None  # two orgs claim it
    assert senders.resolve(app_engine, f"stranger@{_domain()}").org_id is None


def test_a_member_of_several_orgs_is_unknown_with_no_org(app_engine: Engine) -> None:
    domain = _domain()
    org_a, org_b = _org(app_engine, domain), _org(app_engine)
    user = _member(app_engine, org_a, f"both@{domain}")
    _join(app_engine, user, org_b)
    assert senders.resolve(app_engine, f"both@{domain}") == senders.Resolution(
        None, None, None, "unknown_user"
    )
    assert senders.attribute_org(app_engine, f"both@{domain}") is None


def test_quarantine_attribution_rule(app_engine: Engine) -> None:
    domain_a, domain_b = _domain(), _domain()
    org_a = _org(app_engine, domain_a)
    org_b = _org(app_engine, domain_b)
    _member(app_engine, org_a, f"ana@{domain_a}")

    # A forged From of an org-A member fails the check and is still org A's to see.
    kit = mailkit.Kit()
    kit.dmarc(domain_a)
    kit.dmarc("attacker.test", p="none")
    kit.spf("attacker.test", "ip4:203.0.113.0/24")
    raw = kit.dkim(mailkit.message(f"ana@{domain_a}"), "attacker.test")
    auth = verify(raw, "x@attacker.test", "203.0.113.66", "mx.attacker.test",
                  Policy(resolver=kit.zone))
    assert (auth.sender_auth, auth.quarantine_reason) == ("fail", "sender_unverified")
    assert senders.attribute_org(app_engine, auth.sender) == org_a

    assert senders.attribute_org(app_engine, f"stranger@{domain_b}") == org_b
    assert senders.attribute_org(app_engine, f"stranger@{_domain()}") is None
    assert senders.attribute_org(app_engine, None) is None
    assert senders.attribute_org(app_engine, "not-an-address") is None


def test_lookup_functions_are_scan_owned_definers_with_column_grants_only(
    superuser_engine: Engine, app_engine: Engine
) -> None:
    with superuser_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
                " has_function_privilege('public', p.oid, 'EXECUTE'),"
                " has_function_privilege('andai_app', p.oid, 'EXECUTE')"
                " FROM pg_proc p"
                " WHERE p.proname IN ('andai_resolve_sender', 'andai_orgs_for_domain')"
            )
        ).all()
    assert {r[0] for r in rows} == {"andai_resolve_sender", "andai_orgs_for_domain"}
    for name, definer, owner, config, public_may, app_may in rows:
        assert (definer, owner, public_may, app_may) == (True, "andai_scan", False, True), name
        assert config == ["search_path=public, pg_temp"], name

    # The app role still cannot read memberships outside an org context.
    with transaction(app_engine) as tx:
        assert tx.execute(text("SELECT count(*) FROM org_memberships")).scalar_one() == 0


@pytest.mark.parametrize("email", ["", "   ", "@", "nobody"])
def test_resolve_never_matches_empty_or_malformed_addresses(app_engine: Engine, email: str) -> None:
    assert senders.resolve(app_engine, email).reason == "unknown_user"
