"""Focused coverage for workspace CLI request construction."""

from __future__ import annotations

import httpx
import pytest

from awf.cli import workspace_commands
from awf.cli.common import OutputFormat
from awf.db.enums import TaskClass, TaskKind


@pytest.mark.unit
def test_workspace_create_builds_full_v1_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _call(method: str, path: str, **kwargs: object) -> httpx.Response:
        captured["method"] = method
        captured["path"] = path
        captured.update(kwargs)
        return httpx.Response(202, json={"id": "ws"})

    monkeypatch.setattr(workspace_commands, "_call", _call)
    monkeypatch.setattr(workspace_commands, "_handle_response", lambda *_args, **_kwargs: None)

    workspace_commands.workspace_create(
        repo_url="git@example.com:repo/app.git",
        task_title="title",
        task_prompt="prompt",
        branch_base=None,
        task_kind=TaskKind.sync_release_pr.value,
        source_branch="release/source",
        agent="codex",
        model="gpt-5.5",
        effort="xhigh",
        task_class=TaskClass.refactor_task,
        priority=7,
        human_boost=3,
        out_of_scope_changes_json='{"mode":"warn"}',
        provider_recovery_json='{"enabled":true}',
        owned_paths=["src/awf"],
        external_id="task-1",
        cpu=2.0,
        memory="4g",
        steady_state_cpu_cores=1.0,
        steady_state_memory_gb=2.0,
        peak_cpu_cores=3.0,
        peak_memory_gb=6.0,
        disk_mb=1024,
        profile_ref="auto",
        test_commands=["pytest -q"],
        requires_database=True,
        auto_merge=False,
        initial_review_grace_period_seconds=15.0,
        provider_readiness_override=True,
        provider_readiness_override_reason="operator approved",
        idempotency_key="idem-1",
        api_token="token",
        base_url="http://api",
        fmt=OutputFormat.json,
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/workspaces"
    assert captured["base_url"] == "http://api"
    assert captured["headers"] == {"Authorization": "Bearer token", "Idempotency-Key": "idem-1"}
    assert captured["json"] == {
        "repo": {
            "url": "git@example.com:repo/app.git",
            "base_branch": "main",
            "source_branch": "release/source",
        },
        "task": {
            "title": "title",
            "prompt": "prompt",
            "agent": "codex",
            "kind": "sync_release_pr",
            "auto_merge": False,
            "initial_review_grace_period_seconds": 15.0,
            "model": "gpt-5.5",
            "effort": "xhigh",
            "task_class": "refactor_task",
            "external_id": "task-1",
            "priority": 7,
            "human_boost": 3,
            "out_of_scope_changes": {"mode": "warn"},
            "provider_recovery": {"enabled": True},
            "owned_paths": ["src/awf"],
        },
        "workspace": {"profile_ref": "aira", "profile": None},
        "validation": {"commands": ["pytest -q"], "requested_tier": 1},
        "resources": {
            "cpu": 2.0,
            "memory": "4g",
            "steady_state_cpu_cores": 1.0,
            "steady_state_memory_gb": 2.0,
            "peak_cpu_cores": 3.0,
            "peak_memory_gb": 6.0,
            "disk_mb": 1024,
        },
        "preflight": {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "operator approved",
        },
    }


@pytest.mark.unit
def test_workspace_create_builds_minimal_development_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _call(method: str, path: str, **kwargs: object) -> httpx.Response:
        captured["method"] = method
        captured["path"] = path
        captured.update(kwargs)
        return httpx.Response(202, json={"id": "ws"})

    monkeypatch.setattr(workspace_commands, "_call", _call)
    monkeypatch.setattr(workspace_commands, "_handle_response", lambda *_args, **_kwargs: None)

    workspace_commands.workspace_create(
        repo_url="git@example.com:repo/app.git",
        task_title="title",
        task_prompt="prompt",
        branch_base=None,
        task_kind=TaskKind.feature_branch_pr.value,
        source_branch=None,
        agent="codex",
        model=None,
        effort=None,
        task_class=None,
        priority=None,
        human_boost=None,
        out_of_scope_changes_json=None,
        provider_recovery_json=None,
        owned_paths=None,
        external_id=None,
        cpu=None,
        memory=None,
        steady_state_cpu_cores=None,
        steady_state_memory_gb=None,
        peak_cpu_cores=None,
        peak_memory_gb=None,
        disk_mb=None,
        profile_ref="auto",
        test_commands=[],
        requires_database=False,
        auto_merge=True,
        initial_review_grace_period_seconds=None,
        provider_readiness_override=False,
        provider_readiness_override_reason=None,
        idempotency_key=None,
        api_token=None,
        base_url=None,
        fmt=OutputFormat.json,
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/workspaces"
    assert captured["headers"] == {}
    assert captured["json"] == {
        "repo": {"url": "git@example.com:repo/app.git", "base_branch": "development"},
        "task": {
            "title": "title",
            "prompt": "prompt",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "auto_merge": True,
            "initial_review_grace_period_seconds": None,
        },
        "workspace": {"profile_ref": "auto", "profile": None},
        "validation": {"commands": [], "requested_tier": 1},
        "resources": {},
        "preflight": {
            "provider_readiness_override": False,
            "provider_readiness_override_reason": None,
        },
    }
