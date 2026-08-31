"""Shared fixtures for remote unpublished-repair helper tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_unpublished


def _operation(
    *,
    payload: object = None,
    result: object = None,
    status: str = OperationStatus.running.value,
    operation_type: str = OperationType.comment_repair.value,
) -> Operation:
    return Operation(
        id="op",
        workspace_id="ws",
        type=operation_type,
        status=status,
        payload=payload,
        result=result,
    )


def _repair_runner(tmp_path: Path, command_runner: object) -> SimpleNamespace:
    async def _fetch(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    return SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=command_runner),
        _remote_branch_fetch_once=_fetch,
    )


def _repair_worktree(tmp_path: Path, workspace_id: str = "ws_repair") -> Path:
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: test\n", encoding="utf-8")
    return worktree


def _allow_repair_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_verified_awf_comment_repair_worktree",
        lambda **_kwargs: True,
    )

    async def _ownership_ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_repair_unpublished, "repair_agent_runtime_ownership", _ownership_ok)


def _allow_repair_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _comment_provenance(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _no_conflicting_provenance(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_comment_repair_has_operation_provenance",
        _comment_provenance,
    )
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_non_comment_repair_has_operation_provenance",
        _no_conflicting_provenance,
    )


# Production SHAs: published PR head 5c… vs orphaned hosted terminal e7….
_PUBLISHED_PR_HEAD = "5c" * 20
_ORPHANED_HOSTED_TERMINAL = "e7" * 20


def _hosted_orphan_monitor_state() -> MonitorState:
    state = MonitorState(last_push_sha=_ORPHANED_HOSTED_TERMINAL)
    state.hosted_terminal_head_advanced = True
    return state
