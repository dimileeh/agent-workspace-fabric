from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from awf.adapters.base import AgentDefaults
from awf.api.schemas import (
    PullRequestMonitorAdoptionRequest,
    TaskResponse,
    WorkspaceCreateRequest,
    WorkspaceOverviewResponse,
    WorkspaceResponse,
    WorkspaceTask,
)
from awf.common.workspace_policy import (
    CURSOR_AUTO_MODE_POLICY_KEY,
    cursor_auto_mode_from_task_policy,
    cursor_auto_model_selector,
)
from awf.control.executor.helpers import (
    _agent_defaults_for_workspace,
    _agent_identity_model_and_effort,
    _agent_run_model_for_workspace,
)
from awf.db.enums import AgentRuntime, CursorAutoMode
from awf.service.pr_monitor_adoption_helpers import (
    PRMonitorAdoptionError,
    _cursor_auto_mode_provider_preflight,
    _raise_if_agent_policy_conflicts,
    _requested_agent_policy,
)
from awf.service.workspace_observability import agent_identity_payload, effective_agent_identity
from awf.service.workspaces_create import (
    workspace_create_payload_matches,
    workspace_create_task_policy_snapshot,
)


@pytest.mark.parametrize(
    ("mode", "selector"),
    [
        (CursorAutoMode.cost, "auto-smart[optimize_for=cost]"),
        (CursorAutoMode.balance, "auto-smart[optimize_for=balanced]"),
        (CursorAutoMode.intelligence, "auto-smart[optimize_for=intelligence]"),
    ],
)
def test_cursor_auto_mode_maps_product_labels_to_cursor_wire_selector(
    mode: CursorAutoMode,
    selector: str,
) -> None:
    assert cursor_auto_model_selector(mode) == selector
    assert cursor_auto_mode_from_task_policy({CURSOR_AUTO_MODE_POLICY_KEY: mode.value}) is mode


@pytest.mark.parametrize("mode", ["", "balanced", "high", 1, None])
def test_cursor_auto_mode_policy_reader_rejects_noncanonical_values(mode: object) -> None:
    assert cursor_auto_mode_from_task_policy({CURSOR_AUTO_MODE_POLICY_KEY: mode}) is None


def test_workspace_task_accepts_cursor_auto_mode_without_effort_or_fixed_model() -> None:
    task = WorkspaceTask(
        title="Router task",
        prompt="Use Cursor Router",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.intelligence,
    )

    assert task.cursor_auto_mode is CursorAutoMode.intelligence


def test_workspace_task_accepts_explicit_plain_auto_with_cursor_auto_mode() -> None:
    task = WorkspaceTask(
        title="Router task",
        prompt="Use Cursor Router",
        agent=AgentRuntime.cursor,
        model="auto",
        cursor_auto_mode=CursorAutoMode.balance,
    )

    assert task.model == "auto"
    assert task.cursor_auto_mode is CursorAutoMode.balance


@pytest.mark.parametrize(
    "payload",
    [
        {"agent": "codex", "cursor_auto_mode": "cost"},
        {"agent": "cursor", "cursor_auto_mode": "balance", "effort": "xhigh"},
        {"agent": "cursor", "cursor_auto_mode": "intelligence", "model": "gpt-5.6-sol"},
    ],
)
def test_workspace_task_rejects_incompatible_cursor_auto_mode(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="cursor_auto_mode"):
        WorkspaceTask(title="Router task", prompt="Use Cursor Router", **payload)


def test_pr_adoption_rejects_cursor_auto_mode_for_hosted_execution() -> None:
    with pytest.raises(ValidationError, match="hosted"):
        PullRequestMonitorAdoptionRequest(
            pr_url="https://github.com/example/repo/pull/1",
            agent=AgentRuntime.cursor,
            cursor_auto_mode=CursorAutoMode.balance,
            execution={"mode": "hosted"},
        )


def test_create_policy_persists_mode_without_encoding_it_as_generic_effort() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "https://github.com/example/repo.git", "base_branch": "development"},
        task={
            "title": "Router task",
            "prompt": "Use Cursor Router",
            "agent": "cursor",
            "cursor_auto_mode": "balance",
        },
    )

    policy = workspace_create_task_policy_snapshot(request)

    assert policy[CURSOR_AUTO_MODE_POLICY_KEY] == "balance"
    assert "agent_effort" not in policy
    assert "agent_model" not in policy


def test_create_idempotency_compares_persisted_cursor_auto_mode() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "https://github.com/example/repo.git", "base_branch": "development"},
        task={
            "title": "Router task",
            "prompt": "Use Cursor Router",
            "agent": "cursor",
            "cursor_auto_mode": "intelligence",
        },
    )
    policy = workspace_create_task_policy_snapshot(request)
    existing = SimpleNamespace(
        repo_url=request.repo.url,
        branch_base=request.repo.base_branch,
        task_tag=None,
        task_title=request.task.title,
        task_prompt=request.task.prompt,
        task_external_id=None,
        task_class=None,
        owned_paths=[],
        task_policy=policy,
        auto_merge=False,
        initial_review_grace_period_seconds=None,
        agent=AgentRuntime.cursor.value,
        task_kind="feature_branch_pr",
        profile_ref="auto",
        requested_profile=None,
        resolved_profile=None,
        test_commands=[],
        resource_reservations=[],
        requires_database=False,
        env_profile=None,
    )

    assert workspace_create_payload_matches(existing, request)
    existing.task_policy = {**policy, CURSOR_AUTO_MODE_POLICY_KEY: "cost"}
    assert not workspace_create_payload_matches(existing, request)


def test_adoption_policy_persists_mode_without_effort() -> None:
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.cost,
    )

    assert _requested_agent_policy(request) == {CURSOR_AUTO_MODE_POLICY_KEY: "cost"}


def test_adoption_policy_conflict_compares_cursor_auto_mode() -> None:
    workspace = SimpleNamespace(
        id="ws_cursor",
        task_policy={CURSOR_AUTO_MODE_POLICY_KEY: "balance"},
    )
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.intelligence,
    )

    with pytest.raises(PRMonitorAdoptionError) as exc_info:
        _raise_if_agent_policy_conflicts(workspace, request)

    assert exc_info.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
    assert exc_info.value.detail == {
        "workspace_id": "ws_cursor",
        "existing_cursor_auto_mode": "balance",
        "requested_cursor_auto_mode": "intelligence",
    }


def test_executor_resolves_mode_for_initial_and_monitor_recovery_runs() -> None:
    workspace = SimpleNamespace(
        agent=AgentRuntime.cursor.value,
        task_policy={CURSOR_AUTO_MODE_POLICY_KEY: "intelligence"},
    )
    defaults = AgentDefaults(model="auto", effort=None)

    assert _agent_run_model_for_workspace(workspace) == ("auto-smart[optimize_for=intelligence]")
    assert _agent_defaults_for_workspace(workspace, defaults) == AgentDefaults(
        model="auto-smart[optimize_for=intelligence]",
        effort=None,
    )
    assert _agent_identity_model_and_effort(workspace, defaults) == (
        "auto-smart[optimize_for=intelligence]",
        None,
    )


def test_observability_projects_effective_selector_and_explicit_mode_source() -> None:
    identity = effective_agent_identity(
        agent=AgentRuntime.cursor,
        task_policy={CURSOR_AUTO_MODE_POLICY_KEY: "balance"},
    )

    assert identity.model == "auto-smart[optimize_for=balanced]"
    assert identity.model_source == "task_policy"
    assert identity.effort is None


def test_agent_identity_payload_surfaces_product_facing_cursor_auto_mode() -> None:
    workspace = SimpleNamespace(
        agent=AgentRuntime.cursor.value,
        task_policy={CURSOR_AUTO_MODE_POLICY_KEY: "balance"},
    )

    assert agent_identity_payload(workspace)["cursor_auto_mode"] == "balance"


@pytest.mark.parametrize(
    "response_model",
    [WorkspaceResponse, TaskResponse, WorkspaceOverviewResponse],
)
def test_operator_response_models_serialize_cursor_auto_mode(response_model: type[Any]) -> None:
    response = response_model.model_construct(cursor_auto_mode=CursorAutoMode.cost)

    assert response.model_dump(mode="json")["cursor_auto_mode"] == "cost"


@pytest.mark.asyncio
async def test_adoption_cursor_auto_mode_blocks_when_router_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _blocked(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "blocks_launch": True,
            "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
            "message": "Router is unavailable.",
        }

    monkeypatch.setattr(
        "awf.service.workspaces_create._selected_provider_preflight_for_task_async",
        _blocked,
    )
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.intelligence,
    )

    with pytest.raises(PRMonitorAdoptionError) as exc_info:
        await _cursor_auto_mode_provider_preflight(SimpleNamespace(), request)

    assert exc_info.value.error_code == "PROVIDER_READINESS_PRECHECK_FAILED"
    assert exc_info.value.detail == {
        "provider_readiness_preflight": {
            "blocks_launch": True,
            "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
            "message": "Router is unavailable.",
        }
    }
