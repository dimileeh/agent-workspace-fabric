"""Regression tests for sync-base push validation recovery anchors."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner import remote_ops
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult


@pytest.mark.unit
async def test_run_sync_base_threads_operation_start_head_to_validated_push(
    tmp_path: Path,
) -> None:
    """The final sync-base push must validate against the operation start."""

    class _FakeCommandRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def run(self, args: list[str]) -> CommandResult:
            self.calls.append(args)
            return CommandResult(returncode=0, stdout="", stderr="")

    captured_operation_start_heads: list[object] = []

    async def _repair_operation_start_head_result(
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_type: str,
        fallback_head_sha: str | None = None,
    ) -> tuple[str, None]:
        del workspace_id, worktree_path, operation_type, fallback_head_sha
        return "operation-start-sha", None

    async def _resolve_task_tag(_workspace_id: str) -> str | None:
        return None

    async def _fetch_base(**_kwargs: object) -> None:
        return None

    async def _protected_scope_push_block(**_kwargs: object) -> None:
        return None

    async def _validated_git_push_result(**kwargs: object) -> _GitPushResult:
        captured_operation_start_heads.append(kwargs.get("operation_start_head"))
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _repair_operation_start_head_result=_repair_operation_start_head_result,
        _resolve_task_tag=_resolve_task_tag,
        _fetch_base=_fetch_base,
        _protected_scope_push_block=_protected_scope_push_block,
        _validated_git_push_result=_validated_git_push_result,
        _deps=SimpleNamespace(runner=_FakeCommandRunner()),
    )

    result = await remote_ops._run_sync_base(
        runner,
        workspace_id="ws-sync",
        repo=SimpleNamespace(slug=lambda: "owner/repo"),
        pr_number=614,
        base_branch="main",
        remote_branch="awf/ws-sync",
        compose_project="proj",
        compose_file=Path("compose.yml"),
    )

    assert result.pushed is True
    assert captured_operation_start_heads == ["operation-start-sha"]


@pytest.mark.unit
async def test_run_sync_base_threads_compose_context_to_conflict_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conflict repair commits need compose context for protected-scope repair."""

    class _FakeCommandRunner:
        def __init__(self) -> None:
            self.results = [
                CommandResult(returncode=0, stdout="", stderr=""),
                CommandResult(returncode=1, stdout="", stderr="merge conflict"),
                CommandResult(returncode=0, stdout="UU src/conflict.py\n", stderr=""),
            ]

        async def run(self, _args: list[str]) -> CommandResult:
            return self.results.pop(0)

    async def _repair_operation_start_head_result(
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_type: str,
        fallback_head_sha: str | None = None,
    ) -> tuple[str, None]:
        del workspace_id, worktree_path, operation_type, fallback_head_sha
        return "operation-start-sha", None

    async def _resolve_task_tag(_workspace_id: str) -> str | None:
        return None

    async def _fetch_base(**_kwargs: object) -> None:
        return None

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        return False

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return True

    async def _protected_scope_push_block(**_kwargs: object) -> None:
        return None

    async def _validated_git_push_result(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    class _Adapter:
        async def run(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(stdout="", stderr="")

    captured_commit_kwargs: list[dict[str, Any]] = []

    async def _commit_dirty_worktree(**kwargs: Any) -> bool:
        captured_commit_kwargs.append(kwargs)
        return True

    monkeypatch.setattr(
        remote_ops,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _workspace_runtime_context="",
        _repair_operation_start_head_result=_repair_operation_start_head_result,
        _resolve_task_tag=_resolve_task_tag,
        _fetch_base=_fetch_base,
        _provider_recovery_suppresses_cli=_provider_recovery_suppresses_cli,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _protected_scope_push_block=_protected_scope_push_block,
        _validated_git_push_result=_validated_git_push_result,
        _deps=SimpleNamespace(runner=_FakeCommandRunner(), adapter=_Adapter()),
    )

    compose_file = tmp_path / "compose.yml"
    result = await remote_ops._run_sync_base(
        runner,
        workspace_id="ws-sync",
        repo=SimpleNamespace(slug=lambda: "owner/repo"),
        pr_number=614,
        base_branch="main",
        remote_branch="awf/ws-sync",
        compose_project="proj",
        compose_file=compose_file,
    )

    assert result.pushed is True
    assert captured_commit_kwargs == [
        {
            "workspace_id": "ws-sync",
            "message": "fix: resolve PR #614 base conflicts",
            "command_evidence": [],
            "compose_project": "proj",
            "compose_file": compose_file,
            "task_tag": None,
            "operation_start_head": "operation-start-sha",
        }
    ]
