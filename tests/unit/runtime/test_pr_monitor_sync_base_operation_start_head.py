"""Regression tests for sync-base push validation recovery anchors."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.common.commands import CommandResult
from awf.node.git_manager import GitOperationError
from awf.runtime.pr_monitor_runner import remote_ops
from awf.runtime.pr_monitor_runner.constants import _MIRROR_HOOKS_PATH_POISONED_REASON
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
async def test_run_sync_base_repairs_mirror_hooks_before_clean_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean sync-base merge must not run with a poisoned mirror hooks path."""

    events: list[str] = []

    class _FakeCommandRunner:
        async def run(self, args: list[str]) -> CommandResult:
            if "merge" in args:
                events.append("git:" + " ".join(args[args.index("merge") :]))
            return CommandResult(returncode=0, stdout="", stderr="")

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
        events.append("fetch-base")

    async def _protected_scope_push_block(**_kwargs: object) -> None:
        return None

    async def _validated_git_push_result(**_kwargs: object) -> _GitPushResult:
        events.append("validated-push")
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    def _mirror_path_for_worktree(worktree_path: Path) -> Path:
        assert worktree_path == tmp_path / "ws-sync"
        return tmp_path / "mirror.git"

    async def _repair_mirror_hooks_path(mirror_path: Path) -> bool:
        assert mirror_path == tmp_path / "mirror.git"
        events.append("repair-hooks")
        return True

    monkeypatch.setattr(remote_ops, "mirror_path_for_worktree", _mirror_path_for_worktree)
    monkeypatch.setattr(remote_ops, "repair_mirror_hooks_path", _repair_mirror_hooks_path)

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
    assert events == [
        "git:merge --abort",
        "fetch-base",
        "repair-hooks",
        "git:merge --no-edit origin/main",
        "validated-push",
    ]


@pytest.mark.unit
async def test_run_sync_base_mirror_hooks_repair_failure_blocks_clean_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror hook repair failure must stop before the sync-base merge command."""

    events: list[str] = []

    class _FakeCommandRunner:
        async def run(self, args: list[str]) -> CommandResult:
            if "merge" in args:
                events.append("git:" + " ".join(args[args.index("merge") :]))
            return CommandResult(returncode=0, stdout="", stderr="")

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
        events.append("fetch-base")

    async def _unexpected_protected_scope(**_kwargs: object) -> None:
        pytest.fail("sync-base must stop before protected-scope checks")

    async def _unexpected_validated_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("sync-base must stop before validated push")

    def _mirror_path_for_worktree(_worktree_path: Path) -> Path:
        return tmp_path / "mirror.git"

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        events.append("repair-hooks")
        raise GitOperationError(
            operation="mirror.hooks_path_probe",
            returncode=128,
            stdout="",
            stderr="poisoned",
            reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
        )

    monkeypatch.setattr(remote_ops, "mirror_path_for_worktree", _mirror_path_for_worktree)
    monkeypatch.setattr(remote_ops, "repair_mirror_hooks_path", _repair_mirror_hooks_path)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _repair_operation_start_head_result=_repair_operation_start_head_result,
        _resolve_task_tag=_resolve_task_tag,
        _fetch_base=_fetch_base,
        _protected_scope_push_block=_unexpected_protected_scope,
        _validated_git_push_result=_unexpected_validated_push,
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

    assert result.failed is True
    assert result.reason_code == _MIRROR_HOOKS_PATH_POISONED_REASON
    assert "before sync-base merge" in (result.stderr or "")
    assert events == ["git:merge --abort", "fetch-base", "repair-hooks"]


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
