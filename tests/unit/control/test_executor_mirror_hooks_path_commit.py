"""Executor mirror hooks path post-agent commit regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import execution_flow
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.node.git_manager import GitOperationError
from awf.profiles.models import WorkspaceProfile


@pytest.mark.unit
async def test_execute_repairs_mirror_hooks_path_before_post_agent_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_snapshot = WorkspaceProfile(name="mirror-hooks-commit").model_dump(
        mode="json",
        by_alias=True,
    )
    workspace = Workspace(
        id="ws_mirror_hooks_commit",
        status=WorkspaceStatus.running.value,
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        branch_name="awf/ws_mirror_hooks_commit",
        base_commit="abc123",
        task_title="Mirror hooks",
        task_prompt="Repair mirror hooks before post-agent commit.",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=[],
        profile_ref="auto",
        resolved_profile=profile_snapshot,
    )
    mirror_path = tmp_path / "mirror.git"
    mark_failed_calls: list[dict[str, Any]] = []
    repair_calls: list[Path] = []
    deposit_calls: list[dict[str, Any]] = []

    class _Runner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def run(self, args: list[str], **_kwargs: Any) -> CommandResult:
            self.calls.append(args)
            if "diff" in args and "--cached" in args:
                return CommandResult(returncode=0, stdout="src/app.py\n", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

    runner = _Runner()

    class _Validation:
        async def run_profile_phases(self, **_kwargs: Any) -> object:
            return execution_flow.ValidationResult()

        async def run_profile_tool_preflight(self, **_kwargs: Any) -> object:
            return execution_flow.ValidationResult()

    class _Executor:
        _config = SimpleNamespace(
            agent_idle_timeout_seconds=30,
            agent_wall_timeout_seconds=60,
            compose_projects_root=tmp_path / "compose",
            planning_max_iterations_default=6,
            worktrees_root=tmp_path / "worktrees",
        )
        _log_store = None
        _runner = runner
        _usage_sampler = None
        _validation = _Validation()

        async def _begin_execution(self, *_args: object, **_kwargs: object) -> object:
            return workspace, False, False, None

        async def _reject_unsupported_task_kind(self, *_args: object, **_kwargs: object) -> bool:
            return False

        async def _block_open_pr_reexecution_without_recovery(
            self, *_args: object, **_kwargs: object
        ) -> object:
            return SimpleNamespace(blocked=False, recovery=None)

        async def _dispatch_non_feature_task_kind(self, *_args: object, **_kwargs: object) -> bool:
            return False

        async def _prepare_conformance_salvage_for_execution(
            self, *_args: object, **_kwargs: object
        ) -> None:
            return None

        def _defaults_for(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def _mark_failed(self, **kwargs: Any) -> None:
            mark_failed_calls.append(kwargs)

        async def _finish_active_recovery_operations(self, **_kwargs: Any) -> None:
            raise AssertionError("no recovery operation should be finished")

        async def _record_setup_dependency_network_events(self, **_kwargs: Any) -> None:
            return None

        async def _record_runtime_toolchain_findings_safe(self, **_kwargs: Any) -> None:
            return None

        async def _run_agent_git_writability_preflight(self, **_kwargs: Any) -> bool:
            return True

        async def _ensure_ollama_model_or_mark_failed(self, **_kwargs: Any) -> bool:
            return True

        async def _recheck_status(self, *_args: object, **_kwargs: object) -> bool:
            return True

        async def _measure_and_persist_baseline_coverage(self, **_kwargs: Any) -> None:
            return None

        async def _run_agent_task_with_optional_planning(self, **_kwargs: Any) -> None:
            return None

        async def _persist_block_planning_conformance_handoff(
            self, *_args: object, **_kwargs: object
        ) -> None:
            return None

        async def _ensure_worktree_available(self, **_kwargs: Any) -> bool:
            return True

        async def _repair_agent_git_ownership(self, **_kwargs: Any) -> None:
            return None

        async def _refresh_supply_chain_policy_for_workspace(self, **_kwargs: Any) -> object:
            return SimpleNamespace(policy_blocked=False, findings=())

        async def _committed_and_staged_output_is_plan_only(self, **_kwargs: Any) -> bool:
            return False

        async def _protected_file_diffs_for_staged_paths(self, **_kwargs: Any) -> tuple[Any, ...]:
            return ()

        async def _active_operator_grant_specs(
            self, *_args: object, **_kwargs: object
        ) -> list[Any]:
            return []

    async def _repair_agent_runtime_ownership(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _apply_agent_scratch_excludes(**_kwargs: Any) -> None:
        return None

    async def _repair_mirror_hooks_path(path: Path) -> bool:
        repair_calls.append(path)
        if len(repair_calls) == 5:
            raise GitOperationError(
                operation="mirror.hooks_path_repair",
                returncode=128,
                stdout="",
                stderr="could not lock config file\n",
                reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
            )
        return True

    async def _recover_branch_drift(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        execution_flow,
        "get_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(runtime_scratch_paths=()),
    )
    monkeypatch.setattr(
        execution_flow,
        "apply_agent_scratch_excludes",
        _apply_agent_scratch_excludes,
    )
    monkeypatch.setattr(
        execution_flow,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(execution_flow, "mirror_path_for_worktree", lambda _path: mirror_path)
    monkeypatch.setattr(execution_flow, "repair_mirror_hooks_path", _repair_mirror_hooks_path)
    monkeypatch.setattr(execution_flow, "_recover_branch_drift", _recover_branch_drift)
    monkeypatch.setattr(
        execution_flow._planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        lambda *_args, **kwargs: deposit_calls.append(kwargs),
    )

    await execution_flow.execute(_Executor(), workspace.id)

    assert repair_calls == [mirror_path, mirror_path, mirror_path, mirror_path, mirror_path]
    assert deposit_calls
    assert not any("commit" in call for call in runner.calls)
    assert mark_failed_calls == [
        {
            "workspace_id": workspace.id,
            "from_status": WorkspaceStatus.running,
            "failure_reason": FailureReason.infrastructure_failure,
            "message": "could not repair poisoned mirror hooks path before post-agent commit",
            "reason_code": "MIRROR_HOOKS_PATH_REPAIR_FAILED",
        }
    ]


@pytest.mark.unit
async def test_execute_recovers_missing_head_before_agent_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_snapshot = WorkspaceProfile(name="mirror-hooks-agent-cleanup-head").model_dump(
        mode="json",
        by_alias=True,
    )
    workspace = Workspace(
        id="ws_agent_cleanup_head",
        status=WorkspaceStatus.running.value,
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        branch_name="awf/ws_agent_cleanup_head",
        base_commit="abc123",
        task_title="Mirror hooks",
        task_prompt="Recover missing HEAD before agent cleanup failure.",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=[],
        profile_ref="auto",
        resolved_profile=profile_snapshot,
    )
    mirror_path = tmp_path / "mirror.git"
    worktree_path = tmp_path / "worktrees" / workspace.id
    mark_failed_calls: list[dict[str, Any]] = []
    verify_head = AsyncMock(return_value=False)
    recover_head = AsyncMock(return_value=True)
    verify_recovered = AsyncMock(return_value=True)

    class _Validation:
        async def run_profile_phases(self, **_kwargs: Any) -> object:
            return execution_flow.ValidationResult()

        async def run_profile_tool_preflight(self, **_kwargs: Any) -> object:
            return execution_flow.ValidationResult()

    class _Executor:
        _config = SimpleNamespace(
            agent_idle_timeout_seconds=30,
            agent_wall_timeout_seconds=60,
            compose_projects_root=tmp_path / "compose",
            planning_max_iterations_default=6,
            worktrees_root=tmp_path / "worktrees",
        )
        _log_store = None
        _runner = object()
        _usage_sampler = None
        _validation = _Validation()

        async def _begin_execution(self, *_args: object, **_kwargs: object) -> object:
            return workspace, False, False, None

        async def _reject_unsupported_task_kind(self, *_args: object, **_kwargs: object) -> bool:
            return False

        async def _block_open_pr_reexecution_without_recovery(
            self, *_args: object, **_kwargs: object
        ) -> object:
            return SimpleNamespace(blocked=False, recovery=None)

        async def _dispatch_non_feature_task_kind(self, *_args: object, **_kwargs: object) -> bool:
            return False

        async def _prepare_conformance_salvage_for_execution(
            self, *_args: object, **_kwargs: object
        ) -> None:
            return None

        def _defaults_for(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def _mark_failed(self, **kwargs: Any) -> None:
            mark_failed_calls.append(kwargs)

        async def _finish_active_recovery_operations(self, **_kwargs: Any) -> None:
            raise AssertionError("no recovery operation should be finished")

        async def _record_setup_dependency_network_events(self, **_kwargs: Any) -> None:
            return None

        async def _record_runtime_toolchain_findings_safe(self, **_kwargs: Any) -> None:
            return None

        async def _run_agent_git_writability_preflight(self, **_kwargs: Any) -> bool:
            return True

        async def _ensure_ollama_model_or_mark_failed(self, **_kwargs: Any) -> bool:
            return True

        async def _recheck_status(self, *_args: object, **_kwargs: object) -> bool:
            return True

        async def _measure_and_persist_baseline_coverage(self, **_kwargs: Any) -> None:
            return None

        async def _run_agent_task_with_optional_planning(self, **_kwargs: Any) -> None:
            raise ComposeExecCleanupError(
                invocation_id="awf_agent_cleanup",
                source="agent",
                label="agent",
                message="tagged process still running",
            )

        async def _recover_missing_git_head_or_mark_failed(self, **kwargs: Any) -> bool:
            return await recover_head(**kwargs)

        async def _verify_recovered_post_agent_commit_or_mark_failed(self, **kwargs: Any) -> bool:
            return await verify_recovered(**kwargs)

    async def _repair_agent_runtime_ownership(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _apply_agent_scratch_excludes(**_kwargs: Any) -> None:
        return None

    async def _repair_mirror_hooks_path(_path: Path) -> bool:
        return True

    monkeypatch.setattr(
        execution_flow,
        "get_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(runtime_scratch_paths=()),
    )
    monkeypatch.setattr(
        execution_flow,
        "apply_agent_scratch_excludes",
        _apply_agent_scratch_excludes,
    )
    monkeypatch.setattr(
        execution_flow,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(execution_flow, "mirror_path_for_worktree", lambda _path: mirror_path)
    monkeypatch.setattr(execution_flow, "repair_mirror_hooks_path", _repair_mirror_hooks_path)
    monkeypatch.setattr(execution_flow, "verify_head_object_exists", verify_head, raising=False)

    await execution_flow.execute(_Executor(), workspace.id)

    verify_head.assert_awaited_once_with(worktree_path)
    recover_head.assert_awaited_once()
    assert recover_head.await_args.kwargs == {
        "workspace_id": workspace.id,
        "worktree_path": worktree_path,
        "base_commit": workspace.base_commit,
        "branch_name": workspace.branch_name,
        "from_status": WorkspaceStatus.running,
        "stage": "agent_run_cleanup_failure",
        "error": recover_head.await_args.kwargs["error"],
        "task_tag": workspace.task_tag,
    }
    assert isinstance(recover_head.await_args.kwargs["error"], ComposeExecCleanupError)
    verify_recovered.assert_awaited_once_with(
        workspace_id=workspace.id,
        worktree_path=worktree_path,
        base_commit=workspace.base_commit,
        owned_paths=[],
        expected_status=WorkspaceStatus.running,
        execution_owner_id=None,
    )
    assert mark_failed_calls == [
        {
            "workspace_id": workspace.id,
            "from_status": WorkspaceStatus.running,
            "failure_reason": FailureReason.infrastructure_failure,
            "message": (
                "EXEC_PROCESS_CLEANUP_FAILED: agent agent invocation "
                "awf_agent_cleanup: tagged process still running"
            ),
            "reason_code": "EXEC_PROCESS_CLEANUP_FAILED",
        }
    ]
