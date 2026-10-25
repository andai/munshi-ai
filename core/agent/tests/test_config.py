"""Config is a validated contract delivered by env (P14): boot from MUNSHI_*, fail fast, hide secrets."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.spawn import build_worker_env
from shared.config import load_settings
from shared.config import Settings


def test_defaults_boot_clean():
    s = load_settings()
    assert s.agent_profile == "agent"
    assert s.workspace_path == "/workspace"
    assert s.is_secret_present() is False


def test_env_prefix_is_munshi(monkeypatch):
    monkeypatch.setenv("MUNSHI_AGENT_API_PORT", "9001")
    monkeypatch.setenv("MUNSHI_AGENT_IDENTITY_TOKEN", "scoped-jwt")
    s = Settings()
    assert s.agent_api_port == 9001
    assert s.is_secret_present() is True


def test_invalid_port_fails_fast(monkeypatch):
    monkeypatch.setenv("MUNSHI_AGENT_API_PORT", "999999")
    with pytest.raises(ValidationError):
        Settings()


def test_secret_is_not_in_repr(monkeypatch):
    monkeypatch.setenv("MUNSHI_AGENT_IDENTITY_TOKEN", "super-secret-value")
    s = Settings()
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s)


def test_worker_env_matches_runtime_v1_agent_spec():
    """build_worker_env produces exactly the keys of golden runtime.v1/spec-agent.json (P8)."""
    s = load_settings(agent_identity_token="scoped-jwt-token", workspace_ref="main")
    env = build_worker_env(s, "https://git.example.com/acme/company-memory.git")
    assert set(env) == {
        "MUNSHI_AGENT_IDENTITY_TOKEN",
        "MUNSHI_WORKSPACE_REPO",
        "MUNSHI_WORKSPACE_REF",
        "MUNSHI_WORKSPACE_PATH",
    }
    assert env["MUNSHI_WORKSPACE_REPO"] == "https://git.example.com/acme/company-memory.git"
    assert env["MUNSHI_AGENT_IDENTITY_TOKEN"] == "scoped-jwt-token"


def test_worker_env_carries_configured_model():
    s = load_settings(agent_model="deepseek/deepseek-v4-pro")
    env = build_worker_env(s, "https://git.example.com/acme/company-memory.git")
    assert env["MUNSHI_AGENT_MODEL"] == "deepseek/deepseek-v4-pro"
