"""Executor mirror hooks path repair regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from awf.control.executor import execution_flow
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.node.git_manager import GitOperationError
from awf.profiles.models import WorkspaceProfile


@pytest.mark.unit
async def test_execute_fails_before_setup_when_mirror_hooks_path_repair_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_snapshot = WorkspaceProfile(name="mirror-hooks").model_dump(
        mode="json",
        by_alias=True,
    )
    workspace = Workspace(
        id="ws_mirror_hooks",
        status=WorkspaceStatus.running.value,
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        task_title="Mirror hooks",
        task_prompt="Repair mirror hooks before setup.",
        agent=AgentRuntime.codex.value,
        test_commands=[],
        owned_paths=[],
        profile_ref="auto",
        resolved_profile=profile_snapshot,
    )
    mirror_path = tmp_path / "mirror.git"
    mark_failed_calls: list[dict[str, Any]] = []

    class _Validation:
        async def run_profile_phases(self, **_kwargs: Any) -> object:
            raise AssertionError("profile setup should not run after mirror repair failure")

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

    async def _repair_agent_runtime_ownership(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _repair_mirror_hooks_path(path: Path) -> bool:
        assert path == mirror_path
        raise GitOperationError(
            operation="mirror.hooks_path_repair",
            returncode=128,
            stdout="",
            stderr="could not lock config file\n",
            reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
        )

    monkeypatch.setattr(
        execution_flow,
        "get_adapter",
        lambda *_args, **_kwargs: SimpleNamespace(runtime_scratch_paths=()),
    )
    monkeypatch.setattr(
        execution_flow,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        execution_flow,
        "mirror_path_for_worktree",
        lambda _worktree_path: mirror_path,
    )
    monkeypatch.setattr(
        execution_flow,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )

    with structlog.testing.capture_logs() as captured:
        await execution_flow.execute(_Executor(), workspace.id)

    assert {
        "event": "executor.mirror_hooks_path_repair_failed",
        "workspace_id": workspace.id,
        "reason_code": "MIRROR_HOOKS_PATH_REPAIR_FAILED",
        "stderr": "could not lock config file\n",
        "log_level": "warning",
    } in captured

    assert mark_failed_calls == [
        {
            "workspace_id": workspace.id,
            "from_status": WorkspaceStatus.running,
            "failure_reason": FailureReason.infrastructure_failure,
            "message": "could not repair poisoned mirror hooks path before profile setup",
            "reason_code": "MIRROR_HOOKS_PATH_REPAIR_FAILED",
        }
    ]
