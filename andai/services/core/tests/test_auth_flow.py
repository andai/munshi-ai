"""The whole account lifecycle across the API, on a brand-new database."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import Engine, select

from andai_core.auth.invites import create_invite, invite_mail
from andai_core.auth.sessions import COOKIE_NAME
from andai_core.auth.users import disable_user
from andai_core.db.models import OrgMembership
from andai_core.db.session import transaction
from conftest import link_token, make_harness

API = "/api/v1"
OWNER = "founder@example.com"
OWNER_PASSWORD = "founder passphrase one"
MEMBER = "colleague@example.com"
MEMBER_PASSWORD = "colleague passphrase one"
MEMBER_RESET_PASSWORD = "colleague passphrase two"


def test_bootstrap_invite_login_logout_reset_disable(fresh_app_engine: Engine) -> None:
    harness = make_harness(fresh_app_engine)
    mailer, settings = harness.mailer, harness.services.settings

    def browser(ip: str):
        return harness.client(ip=ip)

    def login(client, email: str, password: str) -> int:
        answer = client.post(f"{API}/auth/login", json={"email": email, "password": password})
        return answer.status_code

    # 1. Bootstrap: the first org and its owner; the owner's set-password link arrives by mail.
    setup = browser("198.51.100.1")
    created = setup.post(f"{API}/bootstrap", json={"email": OWNER, "org_name": "Flow Org"})
    assert created.status_code == 201
    org_id = uuid.UUID(created.json()["org_id"])
    owner_link = link_token(mailer.outbox[-1].text, "invite")

    # 2. The owner sets a password from the link and signs in.
    accepted = setup.post(
        f"{API}/auth/invite/accept", json={"token": owner_link, "password": OWNER_PASSWORD}
    )
    assert accepted.status_code == 200
    owner = browser("198.51.100.2")
    assert login(owner, OWNER, OWNER_PASSWORD) == 200
    owner_me = owner.get(f"{API}/me").json()
    assert owner_me["role"] == "owner" and owner_me["org"]["id"] == str(org_id)
    owner_id = uuid.UUID(owner_me["user"]["id"])

    # 3. The owner invites a colleague (internal path; the admin route comes with team management).
    with transaction(fresh_app_engine, org_id=org_id, user_id=owner_id) as tx:
        token = create_invite(
            tx,
            org_id=org_id,
            email=MEMBER,
            role="member",
            invited_by=owner_id,
            settings=settings,
            now=harness.clock(),
        )
    mail = invite_mail(settings, email=MEMBER, org_name="Flow Org", token=token, owner=False)
    asyncio.run(mailer.send(mail))
    member_link = link_token(mailer.outbox[-1].text, "invite")

    # 4. The colleague accepts, becomes active and signs in.
    newcomer = browser("198.51.100.3")
    joined = newcomer.post(
        f"{API}/auth/invite/accept",
        json={"token": member_link, "password": MEMBER_PASSWORD, "display_name": "Col League"},
    )
    assert joined.status_code == 200 and joined.json() == {"status": "active"}
    assert login(newcomer, MEMBER, MEMBER_PASSWORD) == 200
    member_me = newcomer.get(f"{API}/me").json()
    assert member_me["role"] == "member" and member_me["user"]["display_name"] == "Col League"
    member_id = uuid.UUID(member_me["user"]["id"])
    with transaction(fresh_app_engine, org_id=org_id) as tx:
        roles = dict(tx.execute(select(OrgMembership.user_id, OrgMembership.role)).all())
    assert roles == {owner_id: "owner", member_id: "member"}

    # 5. Logout ends the session: the old cookie no longer works.
    old_cookie = newcomer.cookies[COOKIE_NAME]
    assert newcomer.post(f"{API}/auth/logout").status_code == 204
    replay = browser("198.51.100.3")
    replay.cookies.set(COOKIE_NAME, old_cookie)
    assert replay.get(f"{API}/me").status_code == 401

    # 6. A password reset ends every session of that user.
    laptop, phone = browser("198.51.100.4"), browser("198.51.100.5")
    assert login(laptop, MEMBER, MEMBER_PASSWORD) == 200
    assert login(phone, MEMBER, MEMBER_PASSWORD) == 200
    assert laptop.post(f"{API}/auth/password-reset", json={"email": MEMBER}).status_code == 202
    reset_link = link_token(mailer.outbox[-1].text, "reset-password")
    confirmed = browser("198.51.100.6").post(
        f"{API}/auth/password-reset/confirm",
        json={"token": reset_link, "password": MEMBER_RESET_PASSWORD},
    )
    assert confirmed.status_code == 200
    assert laptop.get(f"{API}/me").status_code == 401
    assert phone.get(f"{API}/me").status_code == 401
    assert login(browser("198.51.100.7"), MEMBER, MEMBER_PASSWORD) == 401
    signed_in = browser("198.51.100.8")
    assert login(signed_in, MEMBER, MEMBER_RESET_PASSWORD) == 200

    # 7. Disabling the colleague rejects their existing session and any new login.
    assert signed_in.get(f"{API}/me").status_code == 200
    with transaction(fresh_app_engine, org_id=org_id, user_id=owner_id) as tx:
        disable_user(tx, org_id=org_id, user_id=member_id, actor=owner_id)
    assert signed_in.get(f"{API}/me").status_code == 401
    assert login(browser("198.51.100.9"), MEMBER, MEMBER_RESET_PASSWORD) == 401
    assert owner.get(f"{API}/me").status_code == 200  # the owner is untouched

    # Mail went to the owner (set-password link) and the colleague (invite, then reset) only.
    assert [m.to for m in mailer.outbox] == [OWNER, MEMBER, MEMBER]
