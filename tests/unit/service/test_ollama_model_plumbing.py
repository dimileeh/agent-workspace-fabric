"""End-to-end Ollama ``--model`` plumbing regression (issue #552).

Locks the chain that previously recorded ``model: None``: an explicit OpenCode
model flows ``TaskCreate.model`` → ``policy["agent_model"]`` →
``effective_agent_identity`` → adapter ``_default_model`` → the OpenCode config's
declared ``provider.ollama.models``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awf.adapters.opencode import OpenCodeAdapter, _opencode_config_for_effort
from awf.api.schemas import WorkspaceCreateRequest
from awf.common.commands import FakeCommandRunner
from awf.control.executor.helpers import _agent_defaults_for_workspace
from awf.service.workspace_observability import effective_agent_identity
from awf.service.workspaces_create import workspace_create_task_policy_snapshot

_MODEL = "kimi-k2.7:cloud"


def _opencode_request() -> WorkspaceCreateRequest:
    return WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/ollama.git", "base_branch": "main"},
        task={
            "title": "Run with an explicit Ollama model",
            "prompt": "p",
            "agent": "opencode",
            "kind": "feature_branch_pr",
            "model": _MODEL,
        },
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["pytest -q"], "requested_tier": 1},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "plumbing regression fixture",
        },
    )


@pytest.mark.unit
def test_explicit_ollama_model_threads_end_to_end() -> None:
    policy = workspace_create_task_policy_snapshot(_opencode_request())
    assert policy["agent_model"] == _MODEL

    identity = effective_agent_identity(agent="opencode", task_policy=policy)
    assert identity.model == _MODEL
    assert identity.model_source == "task_policy"

    # The persisted policy resolves into the adapter's default model so the PR
    # monitor (which runs recovery prompts without an explicit model) stays bound
    # to the requested Ollama model rather than drifting back to AWF's default.
    ws = SimpleNamespace(agent="opencode", task_policy=policy)
    defaults = _agent_defaults_for_workspace(ws, None)
    assert defaults is not None
    assert defaults.model == _MODEL

    adapter = OpenCodeAdapter(runner=FakeCommandRunner(), default_model=defaults.model)
    assert adapter._default_model == _MODEL

    config = _opencode_config_for_effort(effort=None, model=adapter._default_model)
    models = config["provider"]["ollama"]["models"]
    assert _MODEL in models
