from __future__ import annotations

from collections.abc import Mapping
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
    canonical_agent_model_for_cursor_auto,
    cursor_auto_mode_from_task_policy,
    cursor_auto_model_selector,
)
from awf.control.executor.helpers import (
    _agent_defaults_for_workspace,
    _agent_identity_model_and_effort,
    _agent_run_model_for_workspace,
)
from awf.db.enums import AgentRuntime, CursorAutoMode
from awf.profiles.models import WorkspaceProfile
from awf.service.pr_monitor_adoption_cursor_preflight import (
    _cursor_auto_mode_provider_preflight,
    run_deferred_cursor_auto_mode_provider_preflight,
)
from awf.service.pr_monitor_adoption_helpers import (
    AGENT_EFFORT_INTENT_POLICY_KEY,
    PRMonitorAdoptionError,
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


def test_canonical_agent_model_treats_plain_auto_as_omitted_with_cursor_auto_mode() -> None:
    assert (
        canonical_agent_model_for_cursor_auto(
            model="auto",
            cursor_auto_mode=CursorAutoMode.balance,
        )
        is None
    )
    assert (
        canonical_agent_model_for_cursor_auto(
            model="  auto  ",
            cursor_auto_mode="cost",
        )
        is None
    )
    assert (
        canonical_agent_model_for_cursor_auto(
            model="auto",
            cursor_auto_mode=None,
        )
        == "auto"
    )
    assert (
        canonical_agent_model_for_cursor_auto(
            model="gpt-5.6-sol",
            cursor_auto_mode=CursorAutoMode.intelligence,
        )
        == "gpt-5.6-sol"
    )


@pytest.mark.parametrize("model", ["", "   ", "\t"])
def test_canonical_agent_model_treats_blank_model_as_omitted(model: str) -> None:
    """Blank/whitespace models normalize like an omitted model for idempotency."""

    assert (
        canonical_agent_model_for_cursor_auto(
            model=model,
            cursor_auto_mode=CursorAutoMode.balance,
        )
        is None
    )
    assert canonical_agent_model_for_cursor_auto(model=model, cursor_auto_mode=None) is None


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


def test_create_policy_omits_agent_model_when_plain_auto_accompanies_cursor_auto_mode() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "https://github.com/example/repo.git", "base_branch": "development"},
        task={
            "title": "Router task",
            "prompt": "Use Cursor Router",
            "agent": "cursor",
            "model": "auto",
            "cursor_auto_mode": "balance",
        },
    )

    policy = workspace_create_task_policy_snapshot(request)

    assert policy[CURSOR_AUTO_MODE_POLICY_KEY] == "balance"
    assert "agent_model" not in policy


def test_create_idempotency_equates_omitted_model_and_plain_auto_with_cursor_auto_mode() -> None:
    omitted = WorkspaceCreateRequest(
        repo={"url": "https://github.com/example/repo.git", "base_branch": "development"},
        task={
            "title": "Router task",
            "prompt": "Use Cursor Router",
            "agent": "cursor",
            "cursor_auto_mode": "intelligence",
        },
    )
    explicit_auto = WorkspaceCreateRequest(
        repo={"url": "https://github.com/example/repo.git", "base_branch": "development"},
        task={
            "title": "Router task",
            "prompt": "Use Cursor Router",
            "agent": "cursor",
            "model": "auto",
            "cursor_auto_mode": "intelligence",
        },
    )
    policy = workspace_create_task_policy_snapshot(omitted)
    existing = SimpleNamespace(
        repo_url=omitted.repo.url,
        branch_base=omitted.repo.base_branch,
        task_tag=None,
        task_title=omitted.task.title,
        task_prompt=omitted.task.prompt,
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

    assert workspace_create_payload_matches(existing, explicit_auto)

    # Legacy rows that already stored agent_model='auto' must still match omit.
    existing.task_policy = {**policy, "agent_model": "auto"}
    assert workspace_create_payload_matches(existing, omitted)


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


def test_adoption_policy_cursor_fixed_model_omits_effort() -> None:
    """Live Cursor defaults omit portable effort; do not persist historical xhigh."""
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        model="gpt-5",
    )

    assert _requested_agent_policy(request) == {"agent_model": "gpt-5"}


def test_adoption_policy_persists_effort_intent_key_for_explicit_and_omitted() -> None:
    from awf.common.github_client import PullRequestAdoptionMetadata, RepoRef
    from awf.service.pr_monitor_adoption_helpers import _adoption_task_policy

    metadata = PullRequestAdoptionMetadata(
        number=1,
        head_ref="feature/x",
        head_repo_slug="example/repo",
        base_ref="main",
        head_sha="a" * 40,
        base_sha="b" * 40,
        state="OPEN",
        is_draft=False,
        closed=False,
        merged=False,
        author="octocat",
        url="https://github.com/example/repo/pull/1",
        title="t",
    )
    repo = RepoRef(owner="example", name="repo")
    explicit = _adoption_task_policy(
        repo=repo,
        metadata=metadata,
        request=PullRequestMonitorAdoptionRequest(
            pr_url="https://github.com/example/repo/pull/1",
            agent=AgentRuntime.cursor,
            model="gpt-5",
            effort="xhigh",
        ),
        repo_url="https://github.com/example/repo",
    )
    omitted = _adoption_task_policy(
        repo=repo,
        metadata=metadata,
        request=PullRequestMonitorAdoptionRequest(
            pr_url="https://github.com/example/repo/pull/1",
            agent=AgentRuntime.cursor,
            model="gpt-5",
        ),
        repo_url="https://github.com/example/repo",
    )

    assert AGENT_EFFORT_INTENT_POLICY_KEY in explicit
    assert explicit[AGENT_EFFORT_INTENT_POLICY_KEY] == "xhigh"
    assert explicit["agent_effort"] == "xhigh"
    assert AGENT_EFFORT_INTENT_POLICY_KEY in omitted
    assert omitted[AGENT_EFFORT_INTENT_POLICY_KEY] is None
    assert "agent_effort" not in omitted


def test_adoption_policy_conflict_equates_legacy_cursor_xhigh_with_omitted_effort() -> None:
    workspace = SimpleNamespace(
        id="ws_cursor_legacy",
        task_policy={"agent_model": "gpt-5", "agent_effort": "xhigh"},
    )
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        model="gpt-5",
        effort=None,
    )

    _raise_if_agent_policy_conflicts(workspace, request)


def test_adoption_policy_conflict_rejects_new_world_explicit_xhigh_when_effort_omitted() -> None:
    """Explicit effort=xhigh is not legacy: omitted replay must conflict."""
    workspace = SimpleNamespace(
        id="ws_cursor_explicit_xhigh",
        task_policy={
            "agent_model": "gpt-5",
            "agent_effort": "xhigh",
            AGENT_EFFORT_INTENT_POLICY_KEY: "xhigh",
        },
    )
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        model="gpt-5",
        effort=None,
    )

    with pytest.raises(PRMonitorAdoptionError) as excinfo:
        _raise_if_agent_policy_conflicts(workspace, request)

    assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
    assert excinfo.value.detail == {
        "workspace_id": "ws_cursor_explicit_xhigh",
        "existing_agent_effort": "xhigh",
        "requested_agent_effort": None,
    }


def test_adoption_policy_conflict_still_rejects_explicit_cursor_effort_mismatch() -> None:
    workspace = SimpleNamespace(
        id="ws_cursor_legacy",
        task_policy={"agent_model": "gpt-5", "agent_effort": "xhigh"},
    )
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        model="gpt-5",
        effort="high",
    )

    with pytest.raises(PRMonitorAdoptionError) as excinfo:
        _raise_if_agent_policy_conflicts(workspace, request)

    assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
    assert excinfo.value.detail == {
        "workspace_id": "ws_cursor_legacy",
        "existing_agent_effort": "xhigh",
        "requested_agent_effort": "high",
    }


def test_adoption_policy_omits_agent_model_when_plain_auto_accompanies_cursor_auto_mode() -> None:
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        model="auto",
        cursor_auto_mode=CursorAutoMode.cost,
    )

    assert _requested_agent_policy(request) == {CURSOR_AUTO_MODE_POLICY_KEY: "cost"}


def test_adoption_policy_conflict_equates_omitted_model_and_plain_auto_with_cursor_auto_mode() -> (
    None
):
    workspace = SimpleNamespace(
        id="ws_cursor",
        task_policy={CURSOR_AUTO_MODE_POLICY_KEY: "cost"},
    )
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        model="auto",
        cursor_auto_mode=CursorAutoMode.cost,
    )

    _raise_if_agent_policy_conflicts(workspace, request)

    # Legacy persisted agent_model='auto' must not conflict with an omitted replay.
    workspace.task_policy = {
        CURSOR_AUTO_MODE_POLICY_KEY: "cost",
        "agent_model": "auto",
    }
    omitted = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.cost,
    )
    _raise_if_agent_policy_conflicts(workspace, omitted)


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


def test_provider_recovery_fixed_model_clears_cursor_auto_mode_for_executor() -> None:
    """Recovery must drop Auto mode when installing a fixed fallback model.

    Otherwise helpers keep preferring ``auto-smart[...]`` and silently ignore the
    selected recovery target (see PR #850 review thread).
    """
    from awf.service.provider_recovery import _install_fixed_recovery_model

    policy = _install_fixed_recovery_model(
        {CURSOR_AUTO_MODE_POLICY_KEY: "intelligence", "keep": True},
        "gpt-5.6-sol",
    )
    workspace = SimpleNamespace(agent=AgentRuntime.cursor.value, task_policy=policy)
    defaults = AgentDefaults(model="auto", effort=None)

    assert CURSOR_AUTO_MODE_POLICY_KEY not in policy
    assert policy["agent_model"] == "gpt-5.6-sol"
    assert policy["keep"] is True
    assert _agent_run_model_for_workspace(workspace) == "gpt-5.6-sol"
    assert _agent_defaults_for_workspace(workspace, defaults) == AgentDefaults(
        model="gpt-5.6-sol",
        effort=None,
    )
    assert _agent_identity_model_and_effort(workspace, defaults) == ("gpt-5.6-sol", None)


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
    """Resolvable inline profiles still fail-fast when Router is unavailable."""

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
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.intelligence,
        profile=WorkspaceProfile(
            name="cursor-profile",
            runtime={"environment": {"CURSOR_API_KEY": "profile-cursor-key"}},
        ),
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


@pytest.mark.asyncio
async def test_adoption_cursor_auto_mode_defers_router_preflight_for_unresolved_auto_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default profile_ref=auto cannot see repo-local CURSOR_API_KEY leases yet.

    Defer even when the worker already has a CURSOR_API_KEY — probing with that
    key would persist a result that skips the provision-time recheck with the
    resolved profile credential (create-path parity).
    """
    called = False

    async def _must_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"blocks_launch": True, "reason_code": "CURSOR_AUTH_MISSING"}

    monkeypatch.setattr(
        "awf.service.workspaces_create._selected_provider_preflight_for_task_async",
        _must_not_run,
    )
    monkeypatch.setenv("CURSOR_API_KEY", "worker-cursor-key")
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.intelligence,
        profile_ref="auto",
    )

    assert await _cursor_auto_mode_provider_preflight(SimpleNamespace(), request) is None
    assert called is False


@pytest.mark.asyncio
async def test_adoption_cursor_auto_mode_preflight_unknown_profile_ref_is_invalid_profile() -> None:
    """Unknown named profile_ref must surface as INVALID_PROFILE, not an internal error."""
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.intelligence,
        profile_ref="missing-profile",
    )

    with pytest.raises(PRMonitorAdoptionError) as raised:
        await _cursor_auto_mode_provider_preflight(SimpleNamespace(), request)

    assert raised.value.error_code == "INVALID_PROFILE"
    assert raised.value.status_code == 422
    assert "missing-profile" in raised.value.message


@pytest.mark.asyncio
async def test_adoption_cursor_auto_mode_preflight_lint_failing_inline_is_invalid_profile() -> None:
    """Inline profile lint failures must map to INVALID_PROFILE with create-parity detail."""
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.intelligence,
        profile={
            "name": "bad-inline",
            "secrets": [
                {
                    "name": "api-token",
                    "kind": "env",
                    "target": "API_TOKEN",
                    "provider": "inline",
                    "ref": "sk-live-do-not-echo",
                }
            ],
        },
    )

    with pytest.raises(PRMonitorAdoptionError) as raised:
        await _cursor_auto_mode_provider_preflight(SimpleNamespace(), request)

    assert raised.value.error_code == "INVALID_PROFILE"
    assert raised.value.status_code == 422
    assert raised.value.detail is not None
    assert raised.value.detail.get("reason_code") == "SECRET_REF_LOOKS_RAW"
    assert "sk-live-do-not-echo" not in raised.value.message


@pytest.mark.asyncio
async def test_adoption_cursor_auto_mode_preflight_overlays_profile_cursor_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile-only CURSOR_API_KEY must reach Router preflight (create/retry parity)."""
    captured: dict[str, object] = {}

    async def _capture(*_args: object, **kwargs: object) -> dict[str, object]:
        captured["provider_environ"] = kwargs.get("provider_environ")
        return {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}

    monkeypatch.setattr(
        "awf.service.workspaces_create._selected_provider_preflight_for_task_async",
        _capture,
    )
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.intelligence,
        profile=WorkspaceProfile(
            name="cursor-profile",
            runtime={"environment": {"CURSOR_API_KEY": "profile-only-cursor-key"}},
        ),
    )

    result = await _cursor_auto_mode_provider_preflight(SimpleNamespace(), request)

    assert result == {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}
    environ = captured["provider_environ"]
    assert isinstance(environ, Mapping)
    assert environ.get("CURSOR_API_KEY") == "profile-only-cursor-key"


@pytest.mark.asyncio
async def test_adoption_cursor_auto_mode_preflight_overlays_profile_cursor_env_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile ``kind=env`` CURSOR_API_KEY lease must overlay like create readiness."""
    captured: dict[str, object] = {}

    async def _capture(*_args: object, **kwargs: object) -> dict[str, object]:
        captured["provider_environ"] = kwargs.get("provider_environ")
        return {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}

    monkeypatch.setattr(
        "awf.service.workspaces_create._selected_provider_preflight_for_task_async",
        _capture,
    )
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("HOST_CURSOR_KEY", "lease-cursor-key")
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/example/repo/pull/1",
        agent=AgentRuntime.cursor,
        cursor_auto_mode=CursorAutoMode.intelligence,
        profile=WorkspaceProfile(
            name="cursor-lease-profile",
            secrets=[
                {
                    "name": "cursor-key",
                    "kind": "env",
                    "target": "CURSOR_API_KEY",
                    "ref": "env/HOST_CURSOR_KEY",
                    "provider": "env",
                }
            ],
        ),
    )

    await _cursor_auto_mode_provider_preflight(SimpleNamespace(), request)

    environ = captured["provider_environ"]
    assert isinstance(environ, Mapping)
    assert environ.get("CURSOR_API_KEY") == "lease-cursor-key"


@pytest.mark.asyncio
async def test_deferred_cursor_auto_preflight_skips_when_already_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _must_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"blocks_launch": True}

    monkeypatch.setattr(
        "awf.service.workspaces_create._selected_provider_preflight_for_task_async",
        _must_not_run,
    )
    result = await run_deferred_cursor_auto_mode_provider_preflight(
        agent=AgentRuntime.cursor,
        task_policy={
            "cursor_auto_mode": "intelligence",
            "provider_readiness_preflight": {"blocks_launch": False},
        },
        resolved_profile={"name": "repo-local"},
    )
    assert result is None
    assert called is False


@pytest.mark.asyncio
async def test_deferred_cursor_auto_preflight_reruns_when_blocking_snapshot_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed blocking snapshot is not a passed gate; reclaim must re-probe."""

    called = False

    async def _probe(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {
            "blocks_launch": True,
            "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
            "message": "Router is unavailable.",
        }

    monkeypatch.setattr(
        "awf.service.workspaces_create._selected_provider_preflight_for_task_async",
        _probe,
    )
    result = await run_deferred_cursor_auto_mode_provider_preflight(
        agent=AgentRuntime.cursor,
        task_policy={
            "cursor_auto_mode": "intelligence",
            "provider_readiness_preflight": {
                "blocks_launch": True,
                "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
            },
        },
        resolved_profile={"name": "repo-local"},
    )
    assert called is True
    assert result == {
        "blocks_launch": True,
        "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
        "message": "Router is unavailable.",
    }


@pytest.mark.asyncio
async def test_deferred_cursor_auto_preflight_skips_without_cursor_auto_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _must_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"blocks_launch": True}

    monkeypatch.setattr(
        "awf.service.workspaces_create._selected_provider_preflight_for_task_async",
        _must_not_run,
    )
    result = await run_deferred_cursor_auto_mode_provider_preflight(
        agent=AgentRuntime.cursor,
        task_policy={},
        resolved_profile={"name": "repo-local"},
    )
    assert result is None
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("task_policy", [None, "legacy-policy", 42])
async def test_deferred_cursor_auto_preflight_skips_non_mapping_policy(
    monkeypatch: pytest.MonkeyPatch,
    task_policy: object,
) -> None:
    """Non-mapping task_policy cannot carry cursor_auto_mode; deferral must no-op."""

    called = False

    async def _must_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"blocks_launch": True}

    monkeypatch.setattr(
        "awf.service.workspaces_create._selected_provider_preflight_for_task_async",
        _must_not_run,
    )
    result = await run_deferred_cursor_auto_mode_provider_preflight(
        agent=AgentRuntime.cursor,
        task_policy=task_policy,  # type: ignore[arg-type]
        resolved_profile={"name": "repo-local"},
    )
    assert result is None
    assert called is False


@pytest.mark.asyncio
async def test_deferred_cursor_auto_preflight_runs_after_resolved_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adoption deferral completes once checkout resolves the repo-local profile."""
    captured: dict[str, object] = {}

    async def _capture(*_args: object, **kwargs: object) -> dict[str, object]:
        captured["provider_environ"] = kwargs.get("provider_environ")
        return {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}

    monkeypatch.setattr(
        "awf.service.workspaces_create._selected_provider_preflight_for_task_async",
        _capture,
    )
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    result = await run_deferred_cursor_auto_mode_provider_preflight(
        agent=AgentRuntime.cursor,
        task_policy={"cursor_auto_mode": "intelligence"},
        resolved_profile={
            "name": "repo-local",
            "runtime": {"environment": {"CURSOR_API_KEY": "resolved-profile-key"}},
        },
    )
    assert result == {"blocks_launch": False, "reason_code": "CURSOR_ROUTER_AVAILABLE"}
    environ = captured["provider_environ"]
    assert isinstance(environ, Mapping)
    assert environ.get("CURSOR_API_KEY") == "resolved-profile-key"


@pytest.mark.asyncio
async def test_deferred_cursor_auto_preflight_surfaces_router_unavailable(
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
    monkeypatch.setenv("CURSOR_API_KEY", "worker-cursor-key")
    result = await run_deferred_cursor_auto_mode_provider_preflight(
        agent=AgentRuntime.cursor,
        task_policy={"cursor_auto_mode": "intelligence"},
        resolved_profile={"name": "repo-local"},
    )
    assert result == {
        "blocks_launch": True,
        "reason_code": "CURSOR_ROUTER_UNAVAILABLE",
        "message": "Router is unavailable.",
    }
