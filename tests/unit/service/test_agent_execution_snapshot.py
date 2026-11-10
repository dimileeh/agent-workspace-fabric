"""Admission snapshots are immutable execution inputs, including fallback choices."""

from types import SimpleNamespace

import pytest

from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS
from awf.api.schemas import WorkspaceCreateRequest
from awf.common.agent_execution import execution_from_policy
from awf.common.config import Settings
from awf.control.executor.helpers import (
    _agent_defaults_for_workspace,
    _agent_identity_model_and_effort,
)
from awf.db.enums import AgentRuntime
from awf.service.workspaces_create import freeze_execution_policy


def request(**task):
    return WorkspaceCreateRequest.model_validate(
        {
            "repo": {"url": "https://github.com/example/repo"},
            "task": {"title": "test", "prompt": "implement", **task},
        }
    )


def test_feature_is_disabled_and_override_cannot_bypass_it():
    settings = Settings(_env_file=None)
    assert settings.third_party_agent_providers_enabled is False
    payload = request(provider="ollama", model="qwen3-coder")
    assert payload.preflight.allow_unverified_model is False
    with pytest.raises(ValueError, match="ADMISSION_DISABLED"):
        freeze_execution_policy(payload, profile=None, enabled=False, environ={})
    payload.preflight.provider_readiness_override = True
    payload.preflight.allow_unverified_model = True
    with pytest.raises(ValueError, match="ADMISSION_DISABLED"):
        freeze_execution_policy(payload, profile=None, enabled=False, environ={})


def test_native_snapshot_and_fallback_provider_are_frozen():
    payload = request(
        provider_recovery={
            "fallbacks": [{"agent": "codex", "provider": "ollama", "model": "qwen3-coder"}]
        }
    )
    with pytest.raises(ValueError, match="ADMISSION_DISABLED"):
        freeze_execution_policy(payload, profile=None, enabled=False, environ={})
    payload.preflight.allow_unverified_model = True
    policy = freeze_execution_policy(payload, profile={}, enabled=True, environ={})
    assert execution_from_policy(policy).effort == "xhigh"
    fallback = policy["provider_recovery"]["fallbacks"][0]
    assert execution_from_policy(fallback).provider == "ollama"
    assert execution_from_policy(fallback).effort is None


def test_executor_helpers_never_fill_native_effort_for_frozen_selection():
    payload = request(provider="ollama", model="qwen3-coder")
    payload.preflight.allow_unverified_model = True
    policy = freeze_execution_policy(payload, profile={}, enabled=True, environ={})
    ws = SimpleNamespace(agent="codex", task_policy=policy)
    defaults = _agent_defaults_for_workspace(ws, DEFAULT_AGENT_DEFAULTS[AgentRuntime.codex])
    assert defaults.effort is None
    assert defaults.execution.provider == "ollama"
    assert _agent_identity_model_and_effort(ws, DEFAULT_AGENT_DEFAULTS[AgentRuntime.codex]) == (
        "qwen3-coder",
        None,
    )


def test_third_party_unknown_pricing_cannot_inherit_native_price():
    from awf.service.workspace_observability import workspace_pricing_metadata

    payload = request(provider="ollama", model="coder")
    payload.preflight.allow_unverified_model = True
    policy = freeze_execution_policy(payload, profile={}, enabled=True, environ={})
    pricing = {
        "provider": "openai",
        "model": "gpt-native",
        "currency": "USD",
        "unit": "per_1m_tokens",
        "price_per_unit": 10,
        "timestamp": "2026-11-09T00:00:00Z",
    }
    workspace = SimpleNamespace(
        id="ws", task_policy=policy, resolved_profile={"pricing": {"pricing": pricing}}
    )
    assert workspace_pricing_metadata(workspace) is None
    pricing.update(provider="ollama", model="coder")
    assert workspace_pricing_metadata(workspace).provider == "ollama"


def test_task_and_attempt_projections_preserve_selected_provider_without_native_effort():
    from datetime import UTC, datetime

    from awf.db.models import TaskAttempt, Workspace
    from awf.service.tasks import _attempt_response, _task_from_workspace

    payload = request(provider="ollama", model="coder")
    payload.preflight.allow_unverified_model = True
    policy = freeze_execution_policy(payload, profile={}, enabled=True, environ={})
    now = datetime.now(UTC)
    ws = Workspace(
        id="ws_selection",
        repo_url="https://example.com/repo",
        branch_base="development",
        task_title="task",
        task_prompt="fix",
        agent="codex",
        status="requested",
        task_policy=policy,
        owned_paths=[],
        created_at=now,
        updated_at=now,
    )
    ws.operations = []
    task = _task_from_workspace(ws)
    assert (
        task.agent_provider == "ollama"
        and task.agent_model == "coder"
        and task.agent_effort is None
    )
    attempt = TaskAttempt(
        id="attempt",
        task_id="task",
        workspace_id=ws.id,
        attempt_number=1,
        agent="codex",
        created_at=now,
        is_canonical_for_merge=True,
    )
    attempt.workspace = ws
    attempt.merge_candidate = None
    response = _attempt_response(attempt)
    assert response.agent_provider == "ollama"
    assert response.agent_execution == policy["agent_execution"]
    assert response.agent_effort is None


@pytest.mark.parametrize("provider", ["ollama", "openrouter"])
def test_auto_profile_connections_are_not_frozen_from_service_defaults(provider):
    payload = request(
        provider=provider,
        model="coder",
        provider_recovery={
            "fallbacks": [
                {"agent": "claude_code", "provider": "ollama", "model": "fallback"},
                {"agent": "codex", "provider": "openrouter", "model": "router/fallback"},
            ]
        },
    )
    payload.preflight.allow_unverified_model = True
    policy = freeze_execution_policy(
        payload,
        profile=None,
        enabled=True,
        environ={"OLLAMA_HOST": "http://service-default:11434"},
    )
    assert "agent_execution" not in policy
    assert policy["agent_execution_profile_pending"] is True
    assert all("agent_execution" not in t for t in policy["provider_recovery"]["fallbacks"])


def test_pending_third_party_never_projects_or_executes_as_native():
    from awf.service.workspace_observability import effective_agent_identity

    policy = {
        "agent_execution_profile_pending": True,
        "agent_provider": "ollama",
        "agent_model": "candidate:cloud",
        "allow_unverified_model": True,
    }
    identity = effective_agent_identity(agent="codex", task_policy=policy)
    assert identity.model == "candidate:cloud"
    assert identity.effort is None
    with pytest.raises(ValueError, match="EXECUTION_PROFILE_BINDING_REQUIRED"):
        _agent_defaults_for_workspace(
            SimpleNamespace(agent="codex", task_policy=policy),
            DEFAULT_AGENT_DEFAULTS[AgentRuntime.codex],
        )
