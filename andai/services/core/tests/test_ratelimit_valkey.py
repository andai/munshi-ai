"""The Valkey limiter (production) against a disposable Valkey, side by side with the in-memory one."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import redis
from sqlalchemy import Engine

from andai_core.auth.ratelimit import MemoryLimiter, ValkeyLimiter, login_key
from conftest import PASSWORD, make_harness, new_member


class FakeTime:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def valkey(valkey_url: str) -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(valkey_url)
    yield client
    client.close()


@pytest.fixture(params=["valkey", "memory"])
def limiter(request: pytest.FixtureRequest, valkey_url: str):
    clock = FakeTime()
    if request.param == "memory":
        return MemoryLimiter(5, 900, clock=clock), clock
    prefix = f"test:{uuid.uuid4().hex}:"
    return ValkeyLimiter(redis.Redis.from_url(valkey_url), 5, 900, clock=clock, prefix=prefix), clock


def test_five_hits_block_until_the_oldest_leaves_the_window(limiter) -> None:
    lim, clock = limiter
    key = login_key("a@example.test", "192.0.2.1")
    for i in range(5):
        assert lim.blocked(key) is None
        lim.hit(key)
        clock.now += 60  # hits at t0, t0+60, ..., t0+240

    assert lim.blocked(key) == 900 - 300  # oldest hit (t0) leaves at t0+900; now is t0+300
    clock.now += 599
    assert lim.blocked(key) == 1
    clock.now += 1
    assert lim.blocked(key) is None  # t0 left the window; four hits remain
    lim.hit(key)
    assert lim.blocked(key) == 60  # now the t0+60 hit is the oldest


def test_keys_are_independent_and_clear_resets(limiter) -> None:
    lim, _ = limiter
    a = login_key("a@example.test", "192.0.2.1")
    b = login_key("a@example.test", "192.0.2.2")
    for _ in range(5):
        lim.hit(a)
    assert lim.blocked(a) is not None
    assert lim.blocked(b) is None
    lim.clear(a)
    assert lim.blocked(a) is None


def test_valkey_keys_are_hashed_and_expire(valkey: redis.Redis, valkey_url: str) -> None:
    prefix = f"test:{uuid.uuid4().hex}:"
    lim = ValkeyLimiter(redis.Redis.from_url(valkey_url), 5, 900, prefix=prefix)
    email = f"{uuid.uuid4().hex}@example.test"
    lim.hit(login_key(email, "192.0.2.1"))

    names = [n.decode() for n in valkey.scan_iter(match=f"{prefix}*")]

    assert len(names) == 1
    assert email.split("@")[0] not in names[0] and "192.0.2.1" not in names[0]
    assert 0 < valkey.ttl(names[0]) <= 900


def test_valkey_limiter_is_shared_between_processes(valkey_url: str) -> None:
    prefix = f"test:{uuid.uuid4().hex}:"
    first = ValkeyLimiter(redis.Redis.from_url(valkey_url), 5, 900, prefix=prefix)
    second = ValkeyLimiter(redis.Redis.from_url(valkey_url), 5, 900, prefix=prefix)
    key = login_key("shared@example.test", "192.0.2.9")
    for limiter in (first, second, first, second, first):
        limiter.hit(key)
    assert second.blocked(key) is not None


def test_login_through_valkey_rate_limits_the_sixth_attempt(
    app_engine: Engine, valkey_url: str
) -> None:
    member = new_member(app_engine)
    harness = make_harness(app_engine)
    harness.services.login_limiter = ValkeyLimiter(
        redis.Redis.from_url(valkey_url), prefix=f"test:{uuid.uuid4().hex}:"
    )
    client = harness.client()

    codes = [
        client.post(
            "/api/v1/auth/login", json={"email": member.email, "password": "wrong-password!"}
        ).status_code
        for _ in range(5)
    ]
    sixth = client.post("/api/v1/auth/login", json={"email": member.email, "password": PASSWORD})

    assert codes == [401] * 5
    assert sixth.status_code == 429
    assert 890 <= int(sixth.headers["retry-after"]) <= 900


def test_valkey_outage_answers_503_not_a_login(app_engine: Engine) -> None:
    member = new_member(app_engine)
    harness = make_harness(app_engine)
    harness.services.login_limiter = ValkeyLimiter(
        redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=1)
    )

    response = harness.client().post(
        "/api/v1/auth/login", json={"email": member.email, "password": PASSWORD}
    )

    assert response.status_code == 503
    assert "set-cookie" not in response.headers
