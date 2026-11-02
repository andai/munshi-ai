"""The minutes language-model client (MOM-01, D-47): schema-constrained calls to the one
env-configured route, typed failures, and a key that never reaches an error or a log line.

Every call goes through `httpx.MockTransport`; no test reaches a real model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from andai_core.llm import (
    LlmClient,
    LlmConfig,
    LlmInvalidOutput,
    LlmNotConfigured,
    LlmTruncated,
    LlmUnavailable,
    load_live_settings,
    strict_schema,
)
from andai_core.settings import Settings

KEY = "sk-test-0123456789abcdef"
BASE = "http://llm.test"


class Thing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    evidence: list[str]
    owner: str | None = None


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    things: list[Thing]


GOOD = {"summary": "ok", "things": [{"text": "a", "evidence": ["seg_1"], "owner": None}]}


def reply(content: object, *, finish: str = "stop", status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "model": "test-model",
            "choices": [
                {"finish_reason": finish, "message": {"role": "assistant", "content": content}}
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        },
    )


class Route:
    """A scripted route: each request gets the next response (or raises it)."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def body(self, i: int = 0) -> dict:
        return json.loads(self.requests[i].content)


def client(route: Route, **config) -> LlmClient:
    cfg = LlmConfig(
        base_url=config.pop("base_url", BASE),
        api_key=config.pop("api_key", SecretStr(KEY)),
        model=config.pop("model", "test-model"),
        **config,
    )
    return LlmClient(cfg, transport=httpx.MockTransport(route))


def call(llm: LlmClient, name: str = "minutes"):
    return asyncio.run(llm.chat_json([{"role": "user", "content": "hi"}], Out, name=name))


# ─── the request ──────────────────────────────────────────────────────────────────────────────────


def test_the_request_is_a_strict_json_schema_chat_call_to_the_configured_route() -> None:
    route = Route(reply(json.dumps(GOOD)))

    result = call(client(route, max_output_tokens=1234))

    request = route.requests[0]
    assert str(request.url) == f"{BASE}/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = route.body()
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 1234
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"] == "minutes"
    assert fmt["json_schema"]["schema"] == strict_schema(Out)
    assert result.data == GOOD
    assert (result.usage.prompt_tokens, result.usage.completion_tokens, result.usage.calls) == (
        11, 7, 1,
    )  # fmt: skip
    assert result.usage.latency_s >= 0


def test_a_base_url_with_v1_or_a_trailing_slash_still_reaches_one_v1() -> None:
    for base in (f"{BASE}/", f"{BASE}/v1", f"{BASE}/v1/"):
        route = Route(reply(json.dumps(GOOD)))
        call(client(route, base_url=base))
        assert str(route.requests[0].url) == f"{BASE}/v1/chat/completions"


def test_no_key_sends_no_authorization_header() -> None:
    route = Route(reply(json.dumps(GOOD)))
    call(client(route, api_key=None))
    assert "Authorization" not in route.requests[0].headers


def test_the_strict_schema_requires_every_property_and_forbids_others() -> None:
    schema = strict_schema(Out)

    def objects(node):
        if isinstance(node, dict):
            if "properties" in node:
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)

    found = list(objects(schema))
    assert len(found) == 2
    for node in found:
        assert node["additionalProperties"] is False
        assert sorted(node["required"]) == sorted(node["properties"])
    assert "default" not in json.dumps(schema)


# ─── typed failures ───────────────────────────────────────────────────────────────────────────────


def test_prose_wrapped_json_gets_one_repair_retry() -> None:
    route = Route(reply(f"Here you go:\n```json\n{json.dumps(GOOD)}\n```"), reply(json.dumps(GOOD)))

    result = call(client(route))

    assert result.data == GOOD
    assert len(route.requests) == 2
    repair = route.body(1)["messages"]
    assert repair[-2]["role"] == "assistant" and repair[-1]["role"] == "user"
    assert result.usage.calls == 2 and result.usage.prompt_tokens == 22


@pytest.mark.parametrize(
    "bad",
    [
        "not json at all",
        json.dumps({"summary": "x"}),  # misses a required field
        json.dumps({**GOOD, "extra": 1}),  # outside the schema
        json.dumps([GOOD]),
        None,
    ],
)
def test_invalid_output_after_the_repair_retry_is_llm_invalid_output(bad) -> None:
    route = Route(reply(bad), reply(bad))

    with pytest.raises(LlmInvalidOutput):
        call(client(route))
    assert len(route.requests) == 2


def test_a_reply_cut_by_the_token_limit_is_llm_truncated_without_retry() -> None:
    route = Route(reply(json.dumps(GOOD)[:20], finish="length"))

    with pytest.raises(LlmTruncated):
        call(client(route))
    assert len(route.requests) == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(503, text="busy"),
        httpx.Response(401, text="no"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
    ],
)
def test_errors_and_timeouts_are_llm_unavailable(failure) -> None:
    with pytest.raises(LlmUnavailable) as caught:
        call(client(Route(failure)))
    if isinstance(failure, httpx.Response):
        assert caught.value.status == failure.status_code


def test_no_route_is_llm_not_configured_and_sends_nothing() -> None:
    route = Route()
    for config in ({"base_url": ""}, {"model": ""}):
        with pytest.raises(LlmNotConfigured):
            call(client(route, **config))
    assert route.requests == []


def test_the_key_and_host_never_reach_an_error_or_a_log_line(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="andai_core.llm")
    errors = []
    for failure in (
        httpx.Response(500, text=f"echo {KEY}"),
        httpx.ConnectError(f"cannot reach {BASE} with {KEY}"),
        reply("prose"),
    ):
        responses = [failure, reply("prose")] if not isinstance(failure, Exception) else [failure]
        try:
            call(client(Route(*responses)))
        except Exception as exc:  # noqa: BLE001 — every failure is inspected
            errors.append(exc)

    assert len(errors) == 3
    text = " ".join(str(e) + repr(e) for e in errors) + caplog.text
    assert KEY not in text
    assert "llm.test" not in text
    assert caplog.records, "calls are logged"
    cfg = LlmConfig(base_url=BASE, api_key=SecretStr(KEY), model="m")
    assert KEY not in repr(cfg) and "llm.test" not in repr(cfg)


# ─── settings and the deployment profile ──────────────────────────────────────────────────────────


def test_settings_carry_the_route_and_the_profile_id_is_stable() -> None:
    settings = Settings(
        ANDAI_LLM_BASE_URL=BASE,
        ANDAI_LLM_API_KEY=KEY,
        ANDAI_LLM_MODEL="m1",
        ANDAI_LLM_CONTEXT_TOKENS="16000",
    )
    cfg = LlmConfig.from_settings(settings)
    assert (cfg.base_url, cfg.model, cfg.context_tokens, cfg.max_output_tokens) == (
        BASE, "m1", 16000, 4000,
    )  # fmt: skip
    assert cfg.timeout_s == 120 and settings.default_timezone == "Asia/Kolkata"
    assert KEY not in repr(settings)
    again = LlmConfig(base_url=f"{BASE}/v1/", api_key=None, model="m1")
    other = LlmConfig(base_url=BASE, api_key=None, model="m2")
    assert cfg.profile_id == again.profile_id != other.profile_id
    assert isinstance(cfg.profile_id, uuid.UUID)


def test_settings_have_no_default_route_and_refuse_an_unknown_zone() -> None:
    settings = Settings()
    assert not LlmConfig.from_settings(settings).configured
    with pytest.raises(ValueError):
        Settings(ANDAI_DEFAULT_TIMEZONE="Mars/Olympus")


# ─── live settings from an env file ───────────────────────────────────────────────────────────────


def test_live_settings_derive_from_the_stacks_route_without_v1(tmp_path: Path) -> None:
    env = tmp_path / "env"
    env.write_text(
        "# a comment\n"
        f"MUNSHI_LLM_BASE_URL={BASE}/v1\n"
        f'MUNSHI_LLM_API_KEY="{KEY}"\n'
        "MUNSHI_LLM_MODEL=route-model\n"
    )

    cfg = load_live_settings(env)

    assert (cfg.base_url, cfg.model) == (BASE, "route-model")
    assert cfg.api_key is not None and cfg.api_key.get_secret_value() == KEY
    assert (cfg.context_tokens, cfg.max_output_tokens) == (32000, 4000)


def test_live_settings_prefer_andai_keys(tmp_path: Path) -> None:
    env = tmp_path / "env"
    env.write_text(
        f"MUNSHI_LLM_BASE_URL={BASE}/v1\nMUNSHI_LLM_API_KEY=other\nMUNSHI_LLM_MODEL=other\n"
        "ANDAI_LLM_BASE_URL=http://andai.test\nANDAI_LLM_API_KEY=k2\nANDAI_LLM_MODEL=m2\n"
        "ANDAI_LLM_CONTEXT_TOKENS=8000\n"
    )

    cfg = load_live_settings(env)

    assert (cfg.base_url, cfg.model, cfg.context_tokens) == ("http://andai.test", "m2", 8000)
    assert cfg.api_key is not None and cfg.api_key.get_secret_value() == "k2"


def test_live_settings_without_a_route_are_not_configured(tmp_path: Path) -> None:
    env = tmp_path / "env"
    env.write_text("SOMETHING=else\n")
    with pytest.raises(LlmNotConfigured):
        load_live_settings(env)


def test_an_empty_key_in_settings_is_no_key() -> None:
    cfg = LlmConfig.from_settings(
        Settings(ANDAI_LLM_BASE_URL=BASE, ANDAI_LLM_API_KEY="", ANDAI_LLM_MODEL="m")
    )
    assert cfg.api_key is None
