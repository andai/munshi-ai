"""Invite rate limits (D-58) on a disposable Valkey and in memory, with a fake clock."""

from __future__ import annotations

import uuid

import pytest
import redis

import mailkit
from andai_intake import limits
from andai_intake.auth.verdict import Policy, SenderAuth, verify

MEMBER = "member@a.example.test"


class FakeTime:
    def __init__(self) -> None:
        self.now = 2_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(params=["valkey", "memory"])
def setup(request: pytest.FixtureRequest) -> tuple[limits.IntakeLimits, FakeTime]:
    clock = FakeTime()
    if request.param == "memory":
        return limits.IntakeLimits.memory(clock=clock), clock
    client = redis.Redis.from_url(request.getfixturevalue("valkey_url"))
    lim = limits.IntakeLimits.valkey(client, clock=clock)
    # A fresh namespace per test: the shared Valkey outlives one test.
    for limiter in (lim.sender, lim.org, lim.peer):
        limiter.prefix = f"test:{uuid.uuid4().hex}:"
    return lim, clock


@pytest.fixture(scope="module")
def checked() -> tuple[SenderAuth, SenderAuth]:
    """A verified message from the member, and a forgery of the same From address."""
    kit = mailkit.Kit()
    kit.dmarc("a.example.test")
    kit.dmarc("attacker.test", p="none")
    real = verify(kit.dkim(mailkit.message(MEMBER), "a.example.test"), "", None, None,
                  Policy(resolver=kit.zone))
    forged = verify(kit.dkim(mailkit.message(MEMBER), "attacker.test"), "", None, None,
                    Policy(resolver=kit.zone))
    assert (real.sender_auth, forged.sender_auth) == ("dmarc_pass", "fail")
    assert real.sender == forged.sender == MEMBER
    return real, forged


def test_the_21st_verified_message_in_an_hour_is_limited_and_the_window_slides(setup) -> None:
    lim, clock = setup
    org = uuid.uuid4()
    for _ in range(20):
        assert lim.admit_verified(MEMBER, org)
        clock.now += 60  # 20 messages over 19 minutes
    assert not lim.admit_verified(MEMBER, org)
    assert lim.admit_verified("other@a.example.test", org)  # the limit is per sender
    clock.now += 3600 - 20 * 60  # the first message leaves the window
    assert lim.admit_verified(MEMBER, org)
    assert not lim.admit_verified(MEMBER, org)  # and only the first


def test_the_201st_verified_message_for_an_org_is_limited(setup) -> None:
    lim, _ = setup
    org, other_org = uuid.uuid4(), uuid.uuid4()
    for n in range(200):
        assert lim.admit_verified(f"m{n // 20}@a.example.test", org), n
    assert not lim.admit_verified("fresh@a.example.test", org)
    assert lim.admit_verified("fresh@a.example.test", other_org)
    # A refusal by the org limit does not use up the sender's own quota.
    assert lim.sender.blocked(limits.sender_key("fresh@a.example.test")) is None


def test_forged_mail_does_not_consume_the_members_limit(setup, checked) -> None:
    lim, _ = setup
    real, forged = checked
    org = uuid.uuid4()
    for _ in range(25):
        assert lim.admit_checked(forged, org)  # not counted: quarantined sender_unverified
    for n in range(20):
        assert lim.admit_checked(real, org), n
    assert not lim.admit_checked(real, org)
    assert lim.admit_checked(real, None)  # no org resolved: quarantined unknown_user, not counted


def test_the_121st_message_from_one_peer_ip_is_refused(setup) -> None:
    lim, clock = setup
    for n in range(120):
        assert lim.admit_peer("203.0.113.66"), n
    assert not lim.admit_peer("203.0.113.66")
    assert lim.admit_peer("203.0.113.67")
    assert lim.admit_peer(None)
    clock.now += 3600
    assert lim.admit_peer("203.0.113.66")


def test_keys_hold_no_address() -> None:
    key = limits.sender_key("Member@A.example.test")
    assert key == limits.sender_key("member@a.example.test")
    assert key.startswith("intake:sender:") and "member" not in key
    assert limits.peer_key("192.0.2.1") == "intake:peer:192.0.2.1"
    org = uuid.uuid4()
    assert limits.org_key(org) == f"intake:org:{org}"


def test_limits_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from andai_intake.settings import IntakeSettings

    settings = IntakeSettings(
        ANDAI_INTAKE_RATE_SENDER_PER_H="2", ANDAI_INTAKE_RATE_ORG_PER_H="3",
        ANDAI_INTAKE_RATE_PEER_PER_H="4", REDIS_URL="redis://127.0.0.1:1/0",
    )
    lim = limits.IntakeLimits.from_settings(settings)
    assert (lim.sender.limit, lim.org.limit, lim.peer.limit) == (2, 3, 4)
    assert all(limiter.window == 3600 for limiter in (lim.sender, lim.org, lim.peer))
