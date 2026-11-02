"""The language model minutes are written with: one OpenAI-compatible route inside the perimeter.

`LlmClient.chat_json(messages, schema, *, name)` posts to `<ANDAI_LLM_BASE_URL>/v1/chat/completions`
with `response_format: {type: json_schema, strict: true}` built from a pydantic model, parses the
reply strictly and validates it against that model. Unparsable or off-schema output gets one repair
retry; after that the call fails with `LlmInvalidOutput`. A reply cut by the token limit fails with
`LlmTruncated` (never stored partly); a network error, timeout or error status fails with
`LlmUnavailable`; no configured route is `LlmNotConfigured`. Error messages and log lines name the
call, the status and the error type only: never the key, the host or any content.

`load_live_settings(env_file)` reads a route from an operator env file for live tools and live
tests only.
"""

from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from andai_core.settings import Settings

log = logging.getLogger(__name__)

CHAT_PATH = "/v1/chat/completions"
# Names the deployment profile ids: uuid5(namespace, "<base url>|<model>").
PROFILE_NAMESPACE = uuid.UUID("6f1c3c39-5b7e-4f57-9c43-6a1d0e0a4b11")
REPAIR_INSTRUCTION = (
    "Your previous reply was not a single JSON object matching the required schema. "
    "Reply again with only that JSON object: no prose, no code fences."
)


# ─── errors ───────────────────────────────────────────────────────────────────────────────────────


class LlmError(Exception):
    """A failed language-model call. `code` is stable; the message names no key, host or content."""

    code = "llm_error"


class LlmNotConfigured(LlmError):
    code = "llm_not_configured"


class LlmUnavailable(LlmError):
    """The route could not be reached, timed out, or answered an error status."""

    code = "llm_unavailable"

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LlmInvalidOutput(LlmError):
    """The reply was not one JSON object of the requested schema, after the repair retry."""

    code = "llm_invalid_output"


class LlmTruncated(LlmError):
    """The reply hit the output token limit (`finish_reason == "length"`)."""

    code = "llm_truncated"


# ─── configuration ────────────────────────────────────────────────────────────────────────────────


def _base(url: str) -> str:
    base = url.strip().rstrip("/")
    return base.removesuffix("/v1")


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: SecretStr | None
    model: str
    context_tokens: int = 32000
    max_output_tokens: int = 4000
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _base(self.base_url))
        object.__setattr__(self, "model", self.model.strip())

    def __repr__(self) -> str:  # the base URL names an internal host
        return f"LlmConfig(model={self.model!r}, configured={self.configured})"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def profile_id(self) -> uuid.UUID:
        """The deployment profile id stored as `llmProfileId`: stable for one route and model."""
        return uuid.uuid5(PROFILE_NAMESPACE, f"{self.base_url}|{self.model}")

    @classmethod
    def from_settings(cls, settings: Settings) -> LlmConfig:
        return cls(
            base_url=settings.llm_base_url_raw,
            api_key=settings.llm_api_key if _secret(settings.llm_api_key) else None,
            model=settings.llm_model,
            context_tokens=settings.llm_context_tokens,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout_s=settings.llm_timeout_s,
        )


def _secret(value: SecretStr | None) -> bool:
    return value is not None and bool(value.get_secret_value().strip())


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_live_settings(env_file: str | Path) -> LlmConfig:
    """The route in an operator env file, for live tools and live tests only.

    `ANDAI_LLM_*` wins; when the file has no `ANDAI_LLM_BASE_URL`, the route the stack already uses
    (`MUNSHI_LLM_BASE_URL` without its trailing `/v1`, `MUNSHI_LLM_API_KEY`, `MUNSHI_LLM_MODEL`)
    is taken in-process. Nothing is printed or written."""
    values = _env_values(Path(env_file))
    prefix = "ANDAI_LLM_" if values.get("ANDAI_LLM_BASE_URL", "").strip() else "MUNSHI_LLM_"
    base, key, model = (
        values.get(f"{prefix}{n}", "").strip() for n in ("BASE_URL", "API_KEY", "MODEL")
    )
    defaults = Settings.model_fields

    def number(name: str, field_name: str, kind: type) -> Any:
        raw = values.get(f"ANDAI_LLM_{name}", "").strip()
        return kind(raw) if raw else defaults[field_name].default

    config = LlmConfig(
        base_url=base,
        api_key=SecretStr(key) if key else None,
        model=model,
        context_tokens=number("CONTEXT_TOKENS", "llm_context_tokens", int),
        max_output_tokens=number("MAX_OUTPUT_TOKENS", "llm_max_output_tokens", int),
        timeout_s=number("TIMEOUT_S", "llm_timeout_s", float),
    )
    if not config.configured:
        raise LlmNotConfigured("the env file names no LLM route (base URL and model)")
    return config


# ─── the schema ───────────────────────────────────────────────────────────────────────────────────

_DROPPED_KEYWORDS = ("title", "default")


def strict_schema(model: type[BaseModel]) -> dict:
    """`model`'s JSON schema (by alias) in the form strict structured output accepts: every
    property required, no additional properties, no titles or defaults."""
    schema = copy.deepcopy(model.model_json_schema(by_alias=True))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key in _DROPPED_KEYWORDS:
                if key in node and not isinstance(node[key], dict):
                    node.pop(key)
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


# ─── the call ─────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Usage:
    """What a call cost, summed over its attempts (a repair retry is a second attempt)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.latency_s += other.latency_s
        self.calls += other.calls


@dataclass
class ChatResult:
    data: dict
    usage: Usage = field(default_factory=Usage)
    model: str = ""


class ChatJson(Protocol):
    """What the generator calls: `LlmClient.chat_json`, or a fake in tests."""

    def __call__(
        self, messages: list[dict], schema: type[BaseModel], *, name: str
    ) -> Awaitable[ChatResult]: ...


def _parse(content: object, schema: type[BaseModel]) -> dict | None:
    if not isinstance(content, str):
        return None
    try:
        value = json.loads(content.strip())
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    try:
        return schema.model_validate(value).model_dump(mode="json", by_alias=True)
    except ValidationError:
        return None


class LlmClient:
    def __init__(self, config: LlmConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport

    @property
    def profile_id(self) -> uuid.UUID:
        return self.config.profile_id

    @property
    def model(self) -> str:
        return self.config.model

    def _body(self, messages: list[dict], schema: dict, name: str) -> dict:
        return {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": self.config.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            },
        }

    async def _post(self, name: str, body: dict) -> tuple[dict, Usage]:
        headers = {}
        if self.config.api_key is not None:
            headers["Authorization"] = f"Bearer {self.config.api_key.get_secret_value()}"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=self.config.base_url,
                headers=headers,
                transport=self._transport,
                timeout=self.config.timeout_s,
            ) as client:
                response = await client.post(CHAT_PATH, json=body)
        except httpx.HTTPError as exc:
            log.warning("llm call %s failed: %s", name, type(exc).__name__)
            raise LlmUnavailable(f"llm call {name}: {type(exc).__name__}") from None
        latency = time.monotonic() - started
        if response.status_code >= 400:
            log.warning("llm call %s answered %s", name, response.status_code)
            raise LlmUnavailable(
                f"llm call {name}: status {response.status_code}", response.status_code
            )
        try:
            payload = response.json()
        except ValueError:
            raise LlmInvalidOutput(f"llm call {name}: the response is not JSON") from None
        if not isinstance(payload, dict):
            raise LlmInvalidOutput(f"llm call {name}: the response is not an object")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        spent = Usage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_s=latency,
            calls=1,
        )
        log.info(
            "llm call %s: status %s, %.2f s, %d prompt + %d completion tokens",
            name, response.status_code, latency, spent.prompt_tokens, spent.completion_tokens,
        )  # fmt: skip
        return payload, spent

    async def chat_json(
        self, messages: list[dict], schema: type[BaseModel], *, name: str
    ) -> ChatResult:
        """One schema-constrained completion, validated against `schema`; one repair retry."""
        if not self.config.configured:
            raise LlmNotConfigured("ANDAI_LLM_BASE_URL and ANDAI_LLM_MODEL are not set")
        json_schema = strict_schema(schema)
        total = Usage()
        conversation = list(messages)
        for attempt in (1, 2):
            payload, spent = await self._post(name, self._body(conversation, json_schema, name))
            total.add(spent)
            choices = payload.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else {}
            choice = choice if isinstance(choice, dict) else {}
            if choice.get("finish_reason") == "length":
                raise LlmTruncated(f"llm call {name}: the reply hit the output token limit")
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            content = message.get("content")
            data = _parse(content, schema)
            if data is not None:
                reported = payload.get("model")
                return ChatResult(
                    data, total, reported if isinstance(reported, str) else self.config.model
                )
            log.warning("llm call %s: attempt %d was not schema-valid JSON", name, attempt)
            conversation = [
                *messages,
                {"role": "assistant", "content": content if isinstance(content, str) else ""},
                {"role": "user", "content": REPAIR_INSTRUCTION},
            ]
        raise LlmInvalidOutput(f"llm call {name}: no schema-valid JSON after one repair retry")


__all__ = [
    "ChatJson",
    "ChatResult",
    "LlmClient",
    "LlmConfig",
    "LlmError",
    "LlmInvalidOutput",
    "LlmNotConfigured",
    "LlmTruncated",
    "LlmUnavailable",
    "Usage",
    "load_live_settings",
    "strict_schema",
]
