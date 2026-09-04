"""Executor mirror hooks path repair regressions after agent failures (part 002)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import execution_flow
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.node.git_manager import GitOperationError
from awf.profiles.models import WorkspaceProfile


@pytest.mark.unit
async def test_execute_repairs_mirror_hooks_path_after_agent_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_snapshot = WorkspaceProfile(name="mirror-hooks-agent-cleanup").model_dump(
        mode="json",
        by_alias=True,
    )
    workspace = Workspace(
        id="ws_mirror_hooks_agent_cleanup",
        status=WorkspaceStatus.running.value,
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        task_title="Mirror hooks",
        task_prompt="Repair mirror hooks after agent cleanup failure.",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=[],
        profile_ref="auto",
        resolved_profile=profile_snapshot,
    )
    mirror_path = tmp_path / "mirror.git"
    mark_failed_calls: list[dict[str, Any]] = []
    repair_calls: list[Path] = []

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
        _agent_runtime_executor = None
        _validation = _Validation()

        async def _begin_execution(self, *_args: object, **_kwargs: object) -> object:
            return workspace, False, False, None

        async def _reject_unsupported_task_kind(self, *_args: object, **_kwargs: object) -> bool:
            return False

        async def _reject_unsupported_agent_runtime(
            self, *_args: object, **_kwargs: object
        ) -> bool:
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

        async def _measure_and_persist_symlink_form_baseline(self, **_kwargs: Any) -> bool | None:
            return _kwargs.get("reuse")

        async def _run_agent_task_with_optional_planning(self, **_kwargs: Any) -> None:
            raise ComposeExecCleanupError(
                invocation_id="awf_agent_cleanup",
                source="agent",
                label="agent",
                message="tagged process still running",
            )

    async def _repair_agent_runtime_ownership(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _apply_agent_scratch_excludes(**_kwargs: Any) -> None:
        return None

    async def _repair_mirror_hooks_path(path: Path) -> bool:
        repair_calls.append(path)
        if len(repair_calls) == 4:
            raise GitOperationError(
                operation="mirror.hooks_path_repair",
                returncode=128,
                stdout="",
                stderr="could not lock config file\n",
                reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
            )
        return True

    monkeypatch.setattr(
        execution_flow,
        "get_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(runtime_scratch_paths=(), is_hosted=False),
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

    await execution_flow.execute(_Executor(), workspace.id)

    assert repair_calls == [mirror_path, mirror_path, mirror_path, mirror_path]
    assert mark_failed_calls == [
        {
            "workspace_id": workspace.id,
            "from_status": WorkspaceStatus.running,
            "failure_reason": FailureReason.infrastructure_failure,
            "message": "could not repair poisoned mirror hooks path after agent cleanup failure",
            "reason_code": "MIRROR_HOOKS_PATH_REPAIR_FAILED",
        }
    ]


@pytest.mark.unit
async def test_execute_repairs_mirror_hooks_path_after_unexpected_agent_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_snapshot = WorkspaceProfile(name="mirror-hooks-agent-unexpected").model_dump(
        mode="json",
        by_alias=True,
    )
    workspace = Workspace(
        id="ws_mirror_hooks_agent_unexpected",
        status=WorkspaceStatus.running.value,
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        task_title="Mirror hooks",
        task_prompt="Repair mirror hooks after unexpected agent failure.",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=[],
        profile_ref="auto",
        resolved_profile=profile_snapshot,
    )
    mirror_path = tmp_path / "mirror.git"
    mark_failed_calls: list[dict[str, Any]] = []
    repair_calls: list[Path] = []

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
        _agent_runtime_executor = None
        _validation = _Validation()

        async def _begin_execution(self, *_args: object, **_kwargs: object) -> object:
            return workspace, False, False, None

        async def _reject_unsupported_task_kind(self, *_args: object, **_kwargs: object) -> bool:
            return False

        async def _reject_unsupported_agent_runtime(
            self, *_args: object, **_kwargs: object
        ) -> bool:
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

        async def _measure_and_persist_symlink_form_baseline(self, **_kwargs: Any) -> bool | None:
            return _kwargs.get("reuse")

        async def _run_agent_task_with_optional_planning(self, **_kwargs: Any) -> None:
            raise RuntimeError("boom: unexpected agent-run failure")

    async def _repair_agent_runtime_ownership(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _apply_agent_scratch_excludes(**_kwargs: Any) -> None:
        return None

    async def _repair_mirror_hooks_path(path: Path) -> bool:
        repair_calls.append(path)
        return True

    monkeypatch.setattr(
        execution_flow,
        "get_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(runtime_scratch_paths=(), is_hosted=False),
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

    await execution_flow.execute(_Executor(), workspace.id)

    assert repair_calls == [mirror_path, mirror_path, mirror_path, mirror_path]
    assert mark_failed_calls == [
        {
            "workspace_id": workspace.id,
            "from_status": WorkspaceStatus.running,
            "failure_reason": FailureReason.infrastructure_failure,
            "message": "unexpected error during agent run: RuntimeError('boom: "
            "unexpected agent-run failure')",
        }
    ]
