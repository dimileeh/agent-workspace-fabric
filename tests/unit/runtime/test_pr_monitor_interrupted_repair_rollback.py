"""Recovery of unpublished comment-repair commits without output salvage."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_unpublished
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
    _git_env_for_merge_safety_object_lookup,
)
from awf.runtime.worktree_writer_lock import (
    exclusive_worktree_writer_lock,
    is_worktree_writer_lock_held,
)


class _RollbackCommandRunner:
    def __init__(
        self,
        *,
        remote_head: str,
        local_head: str,
        local_behind_remote: bool = False,
        ancestry: dict[tuple[str, str], bool] | None = None,
        head_advance_after_ancestry: str | None = None,
        head_advance_after_reset: str | None = None,
        dirty_before_reset: bool = False,
        dirty_after_reset: bool = False,
    ) -> None:
        self.remote_head = remote_head
        self.local_head = local_head
        self.local_behind_remote = local_behind_remote
        self.ancestry = ancestry
        self.head_advance_after_ancestry = head_advance_after_ancestry
        self.head_advance_after_reset = head_advance_after_reset
        self.dirty_before_reset = dirty_before_reset
        self.dirty_after_reset = dirty_after_reset
        self._ancestry_checked = False
        self._reset_done = False
        self.calls: list[tuple[str, ...]] = []

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        if ancestor == descendant:
            return True
        if self.ancestry is not None:
            return self.ancestry.get((ancestor, descendant), False)
        return True

    async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if "rev-parse" in call:
            ref = call[call.index("rev-parse") + 1]
            if ref == "FETCH_HEAD":
                head = self.remote_head
            elif self._reset_done and self.head_advance_after_reset is not None:
                head = self.head_advance_after_reset
            elif self._ancestry_checked and self.head_advance_after_ancestry is not None:
                head = self.head_advance_after_ancestry
            else:
                head = self.local_head
            return CommandResult(returncode=0, stdout=f"{head}\n", stderr="")
        if "merge-base" in call and "--is-ancestor" in call:
            self._ancestry_checked = True
            ancestor_ref = call[call.index("--is-ancestor") + 1]
            descendant_ref = call[call.index("--is-ancestor") + 2]
            if self.ancestry is None and self.local_behind_remote:
                remote_refs = {"FETCH_HEAD", self.remote_head}
                if ancestor_ref in remote_refs and descendant_ref == "HEAD":
                    return CommandResult(returncode=1, stdout="", stderr="")
                if ancestor_ref == "HEAD" and descendant_ref in remote_refs:
                    return CommandResult(returncode=0, stdout="", stderr="")
            ancestor = ancestor_ref
            descendant = descendant_ref
            if ancestor == "FETCH_HEAD":
                ancestor = self.remote_head
            if descendant == "FETCH_HEAD":
                descendant = self.remote_head
            if ancestor == "HEAD":
                ancestor = self.local_head
            if descendant == "HEAD":
                descendant = self.local_head
            return CommandResult(
                returncode=0 if self._is_ancestor(ancestor, descendant) else 1,
                stdout="",
                stderr="",
            )
        if "diff" in call:
            return CommandResult(returncode=0, stdout="M\0src/example.py\0", stderr="")
        if "reset" in call:
            self._reset_done = True
            self.local_head = self.remote_head
            return CommandResult(returncode=0, stdout="", stderr="")
        if "status" in call:
            if not self._reset_done and self._ancestry_checked and self.dirty_before_reset:
                return CommandResult(returncode=0, stdout=" M\0src/example.py\0", stderr="")
            if self._reset_done and self.dirty_after_reset:
                return CommandResult(returncode=0, stdout=" M\0src/example.py\0", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {call}")


def _runner(tmp_path: Path, command_runner: _RollbackCommandRunner) -> SimpleNamespace:
    async def _fetch(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    return SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=command_runner),
        _remote_branch_fetch_once=_fetch,
    )


@pytest.fixture(autouse=True)
def _comment_repair_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _has_comment_provenance(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _has_conflicting_provenance(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_comment_repair_has_operation_provenance",
        _has_comment_provenance,
    )
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_non_comment_repair_has_operation_provenance",
        _has_conflicting_provenance,
    )


@pytest.fixture(autouse=True)
def _verified_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_verified_awf_comment_repair_worktree",
        lambda **_kwargs: True,
        raising=False,
    )

    async def _ownership_ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_repair_unpublished, "repair_agent_runtime_ownership", _ownership_ok)


@pytest.fixture
def real_recovery_reset_lock() -> bool:
    """Opt into the real cross-process writer-lock recovery primitive."""
    return True


@pytest.fixture(autouse=True)
def _delegate_recovery_reset_to_runner(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    if "real_recovery_reset_lock" in request.fixturenames:
        return

    async def _delegate(
        runner: Any,
        *,
        worktree_path: Path,
        pinned_head: str,
        reset_target: str,
        git_env: Mapping[str, str],
    ) -> remote_repair_unpublished._RecoveryResetOutcome:
        (
            ready,
            live_head,
            worktree_dirty,
        ) = await remote_repair_unpublished._live_worktree_ready_for_recovery_reset(
            runner,
            worktree_path=worktree_path,
            pinned_head=pinned_head,
            git_env=git_env,
        )
        if not ready:
            return remote_repair_unpublished._RecoveryResetOutcome(
                ready=False,
                live_head=live_head,
                worktree_dirty=worktree_dirty,
                reset_ok=False,
            )
        reset = await runner.run(
            git_worktree_command(worktree_path, "reset", "--hard", reset_target),
            env=git_env,
        )
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=True,
            live_head=live_head,
            worktree_dirty=False,
            reset_ok=reset.ok,
            reset_stderr=(reset.stderr or "")[:400],
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _delegate,
    )


@pytest.mark.unit
async def test_unpublished_descendant_is_reset_to_verified_remote_head(tmp_path: Path) -> None:
    workspace_id = "ws_interrupted"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    abandoned_head = "b" * 40
    commands = _RollbackCommandRunner(remote_head=remote_head, local_head=abandoned_head)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=abandoned_head,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == remote_head
    assert commands.local_head == remote_head
    reset_calls = [call for call in commands.calls if "reset" in call]
    assert len(reset_calls) == 1
    assert reset_calls[0][-2:] == ("--hard", remote_head)


@pytest.mark.unit
async def test_unpublished_abandon_reconciles_orphaned_hosted_last_push_sha(
    tmp_path: Path,
) -> None:
    """Cross-cycle hosted orphan: abandon reset must align push-tracking to 5c."""
    from awf.runtime.hosted_pr_identity import hosted_pr_identity_for_workspace

    workspace_id = "ws_hosted_orphan"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    published_head = "5c" * 20
    orphaned_terminal = "e7" * 20
    commands = _RollbackCommandRunner(
        remote_head=published_head,
        local_head=orphaned_terminal,
    )
    state = MonitorState(last_push_sha=orphaned_terminal)
    state.hosted_terminal_head_advanced = True

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=published_head,
        local_head=orphaned_terminal,
        state=state,
    )

    assert result is None
    assert restored_head == published_head
    assert state.last_push_sha == published_head
    assert state.hosted_terminal_head_advanced is False
    workspace = SimpleNamespace(
        repo_url="https://github.com/example/repo",
        pr_url="https://github.com/example/repo/pull/9",
        pr_number=9,
        branch_base="main",
        remote_push_branch="awf/ws_hosted_orphan",
        owned_paths=[],
        task_policy={},
        monitor_last_commit_sha=orphaned_terminal,
    )
    assert (
        hosted_pr_identity_for_workspace(workspace, state=state)["expected_head_sha"]
        == published_head
    )


@pytest.mark.unit
async def test_behind_remote_head_fast_forwards_without_failure(tmp_path: Path) -> None:
    workspace_id = "ws_behind"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "c" * 40
    stale_local_head = "a" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=stale_local_head,
        local_behind_remote=True,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=stale_local_head,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == remote_head
    assert commands.local_head == remote_head
    reset_calls = [call for call in commands.calls if "reset" in call]
    assert len(reset_calls) == 1
    assert reset_calls[0][-2:] == ("--hard", remote_head)
    assert all("diff" not in call for call in commands.calls)


@pytest.mark.unit
async def test_behind_remote_ff_reconciles_orphaned_hosted_last_push_sha(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_behind_hosted"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    published_head = "5c" * 20
    stale_local_head = "aa" * 20
    orphaned_terminal = "e7" * 20
    commands = _RollbackCommandRunner(
        remote_head=published_head,
        local_head=stale_local_head,
        local_behind_remote=True,
    )
    state = MonitorState(last_push_sha=orphaned_terminal)
    state.hosted_terminal_head_advanced = True

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=published_head,
        local_head=stale_local_head,
        state=state,
    )

    assert result is None
    assert restored_head == published_head
    assert state.last_push_sha == published_head
    assert state.hosted_terminal_head_advanced is False


@pytest.mark.unit
async def test_unpublished_abandon_race_preserves_orphaned_hosted_last_push_sha(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_hosted_orphan_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    published_head = "5c" * 20
    orphaned_terminal = "e7" * 20
    commands = _RollbackCommandRunner(
        remote_head=published_head,
        local_head=orphaned_terminal,
        head_advance_after_ancestry="dd" * 20,
    )
    state = MonitorState(last_push_sha=orphaned_terminal)
    state.hosted_terminal_head_advanced = True

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=published_head,
        local_head=orphaned_terminal,
        state=state,
    )

    assert restored_head == orphaned_terminal
    assert result is not None
    assert result.failed is True
    assert state.last_push_sha == orphaned_terminal
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_behind_remote_ff_race_preserves_orphaned_hosted_last_push_sha(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_behind_hosted_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    published_head = "5c" * 20
    stale_local_head = "aa" * 20
    orphaned_terminal = "e7" * 20
    commands = _RollbackCommandRunner(
        remote_head=published_head,
        local_head=stale_local_head,
        local_behind_remote=True,
        dirty_before_reset=True,
    )
    state = MonitorState(last_push_sha=orphaned_terminal)
    state.hosted_terminal_head_advanced = True

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=published_head,
        local_head=stale_local_head,
        state=state,
    )

    assert restored_head == stale_local_head
    assert result is not None
    assert result.failed is True
    assert state.last_push_sha == orphaned_terminal
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_unpublished_abandon_post_reset_race_preserves_orphaned_hosted_last_push_sha(
    tmp_path: Path,
) -> None:
    """Post-reset HEAD advance must not reconcile push-tracking outside the lock gap."""
    workspace_id = "ws_post_reset_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    published_head = "5c" * 20
    orphaned_terminal = "e7" * 20
    advanced_head = "dd" * 20
    commands = _RollbackCommandRunner(
        remote_head=published_head,
        local_head=orphaned_terminal,
        head_advance_after_reset=advanced_head,
    )
    state = MonitorState(last_push_sha=orphaned_terminal)
    state.hosted_terminal_head_advanced = True

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=published_head,
        local_head=orphaned_terminal,
        state=state,
    )

    assert restored_head == orphaned_terminal
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"
    assert state.last_push_sha == orphaned_terminal
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_behind_remote_ff_post_reset_race_preserves_orphaned_hosted_last_push_sha(
    tmp_path: Path,
) -> None:
    """Behind-remote FF must recheck HEAD under lock before clearing hosted orphan markers."""
    workspace_id = "ws_behind_post_reset_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    published_head = "5c" * 20
    stale_local_head = "aa" * 20
    orphaned_terminal = "e7" * 20
    advanced_head = "dd" * 20
    commands = _RollbackCommandRunner(
        remote_head=published_head,
        local_head=stale_local_head,
        local_behind_remote=True,
        head_advance_after_reset=advanced_head,
    )
    state = MonitorState(last_push_sha=orphaned_terminal)
    state.hosted_terminal_head_advanced = True

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=published_head,
        local_head=stale_local_head,
        state=state,
    )

    assert restored_head == stale_local_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"
    assert state.last_push_sha == orphaned_terminal
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_behind_remote_fast_forward_refuses_when_head_advances_before_reset(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_behind_head_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "c" * 40
    stale_local_head = "a" * 40
    advanced_head = "d" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=stale_local_head,
        local_behind_remote=True,
        head_advance_after_ancestry=advanced_head,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=stale_local_head,
        state=MonitorState(),
    )

    assert restored_head == stale_local_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_unpublished_descendant_refuses_when_head_advances_before_reset(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_descendant_head_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    abandoned_head = "b" * 40
    advanced_head = "e" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=abandoned_head,
        head_advance_after_ancestry=advanced_head,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=abandoned_head,
        state=MonitorState(),
    )

    assert restored_head == abandoned_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_behind_remote_fast_forward_refuses_when_worktree_becomes_dirty_before_reset(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_behind_dirty_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "c" * 40
    stale_local_head = "a" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=stale_local_head,
        local_behind_remote=True,
        dirty_before_reset=True,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=stale_local_head,
        state=MonitorState(),
    )

    assert restored_head == stale_local_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert "dirty" in result.stderr.lower()
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_unpublished_descendant_refuses_when_worktree_becomes_dirty_before_reset(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_descendant_dirty_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    abandoned_head = "b" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=abandoned_head,
        dirty_before_reset=True,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=abandoned_head,
        state=MonitorState(),
    )

    assert restored_head == abandoned_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert "dirty" in result.stderr.lower()
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_already_published_local_head_supersedes_stale_snapshot(tmp_path: Path) -> None:
    workspace_id = "ws_published"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    stale_snapshot = "a" * 40
    published_head = "b" * 40
    commands = _RollbackCommandRunner(
        remote_head=published_head,
        local_head=published_head,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=stale_snapshot,
        local_head=published_head,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == published_head
    assert any(
        "merge-base" in call and call[-3:] == ("--is-ancestor", stale_snapshot, published_head)
        for call in commands.calls
    )
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_stale_snapshot_remote_advance_resets_unpublished_repairs(tmp_path: Path) -> None:
    workspace_id = "ws_stale_snapshot"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    stale_snapshot = "a" * 40
    advanced_remote = "c" * 40
    unpublished_repair = "b" * 40
    commands = _RollbackCommandRunner(
        remote_head=advanced_remote,
        local_head=unpublished_repair,
        ancestry={
            (stale_snapshot, advanced_remote): True,
            (stale_snapshot, unpublished_repair): True,
            (advanced_remote, unpublished_repair): False,
            (unpublished_repair, advanced_remote): False,
        },
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=stale_snapshot,
        local_head=unpublished_repair,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == advanced_remote
    assert commands.local_head == advanced_remote
    reset_calls = [call for call in commands.calls if "reset" in call]
    assert len(reset_calls) == 1
    assert reset_calls[0][-2:] == ("--hard", advanced_remote)


@pytest.mark.unit
async def test_remote_head_mismatch_fails_without_reset(tmp_path: Path) -> None:
    workspace_id = "ws_mismatch"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    expected = "a" * 40
    fetched = "c" * 40
    local = "b" * 40
    commands = _RollbackCommandRunner(
        remote_head=fetched,
        local_head=local,
        ancestry={
            (expected, fetched): True,
            (expected, local): False,
            (fetched, local): False,
            (local, fetched): False,
        },
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=expected,
        local_head=local,
        state=MonitorState(),
    )

    assert restored_head == local
    assert result is not None
    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_awaiting_workflow_scope_repair_is_never_reset(tmp_path: Path) -> None:
    workspace_id = "ws_workflow_scope"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    commands = _RollbackCommandRunner(remote_head="a" * 40, local_head="b" * 40)
    state = MonitorState()
    state.mark_awaiting_workflow_scope()

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head="b" * 40,
        state=state,
    )

    assert result is None
    assert restored_head == "b" * 40
    assert commands.calls == []


@pytest.mark.unit
async def test_preserved_protected_flow_is_never_reset(tmp_path: Path) -> None:
    workspace_id = "ws_preserved"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    commands = _RollbackCommandRunner(remote_head="a" * 40, local_head="b" * 40)
    state = MonitorState()
    state.mark_addressed("__awf_protected_block_preserved_head__", "b" * 40)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head="b" * 40,
        state=state,
    )

    assert result is None
    assert restored_head == "b" * 40
    assert commands.calls == []


@pytest.mark.unit
async def test_ci_repair_unpublished_commit_is_not_reset_without_comment_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_ci_repair_unpublished"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    ci_repair_head = "b" * 40
    commands = _RollbackCommandRunner(remote_head=remote_head, local_head=ci_repair_head)

    async def _no_comment_provenance(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _ci_repair_provenance(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_comment_repair_has_operation_provenance",
        _no_comment_provenance,
    )
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_non_comment_repair_has_operation_provenance",
        _ci_repair_provenance,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=ci_repair_head,
        state=MonitorState(),
        current_operation_id="op_comment_repair_current",
    )

    assert restored_head == ci_repair_head
    assert commands.local_head == ci_repair_head
    assert all("reset" not in call for call in commands.calls)
    assert result is not None
    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING"


@pytest.mark.unit
def test_operation_result_was_pushed_for_succeeded_ci_repair_outcome() -> None:
    from awf.db.enums import OperationStatus
    from awf.db.models import Operation

    operation = Operation(
        id="op_ci",
        workspace_id="ws",
        type="ci_repair",
        status=OperationStatus.succeeded.value,
        result={"outcome": "ci_repair_pushed", "pushed": True},
    )
    assert remote_repair_unpublished._operation_result_was_pushed(operation) is True


@pytest.mark.unit
def test_is_operator_hint_repair_operation_matches_comment_repair_payload_action() -> None:
    operation = Operation(
        id="op_hint",
        workspace_id="ws",
        type=OperationType.comment_repair.value,
        status=OperationStatus.running.value,
        payload={"action": "operator_hint_repair", "source_head_sha": "a" * 40},
    )
    assert remote_repair_unpublished._is_operator_hint_repair_operation(operation) is True


@pytest.mark.unit
def test_is_operator_hint_repair_operation_rejects_plain_comment_repair() -> None:
    operation = Operation(
        id="op_comment",
        workspace_id="ws",
        type=OperationType.comment_repair.value,
        status=OperationStatus.running.value,
        payload={"action": "comment_repair", "source_head_sha": "a" * 40},
    )
    assert remote_repair_unpublished._is_operator_hint_repair_operation(operation) is False


@pytest.mark.unit
async def test_non_linked_worktree_does_not_enter_rollback_path(tmp_path: Path) -> None:
    workspace_id = "ws_plain_directory"
    (tmp_path / workspace_id).mkdir()
    local_head = "b" * 40
    commands = _RollbackCommandRunner(remote_head="a" * 40, local_head=local_head)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head=local_head,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == local_head
    assert commands.calls == []


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_fetch_head(repo: Path, sha: str, *, branch: str = "fix/review") -> None:
    """Set FETCH_HEAD without ``git update-ref`` (rejected for pseudorefs since Git 2.55)."""
    git_dir = repo / ".git"
    if git_dir.is_file():
        git_dir = Path(git_dir.read_text(encoding="utf-8").split(":", 1)[1].strip())
    fetch_head = git_dir / "FETCH_HEAD"
    fetch_head.parent.mkdir(parents=True, exist_ok=True)
    fetch_head.write_text(
        f"{sha}\tnot-for-merge\tbranch '{branch}' of local test remote\n",
        encoding="utf-8",
    )


def _init_repo_with_lateral_and_remote(worktree: Path) -> tuple[Path, str, str, str]:
    """Return ``(repo, ancestor_sha, remote_sha, lateral_sha)``."""
    repo = worktree
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    _git(repo, "config", "advice.graftFileDeprecated", "false")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "ancestor")
    ancestor = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "remote tip")
    remote = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "--orphan", "lateral", "-q")
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "lateral tip")
    lateral = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, ancestor, remote, lateral


@pytest.mark.unit
async def test_recovery_ancestry_checks_use_merge_safety_git_env(tmp_path: Path) -> None:
    """Ancestry and diff checks must ignore replace refs and graft overrides."""
    workspace_id = "ws_merge_safety_env"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    local_head = "b" * 40
    captured_envs: list[dict[str, str] | None] = []

    class _EnvCapturingRunner(_RollbackCommandRunner):
        async def run(self, args: list[str], **kwargs: object) -> CommandResult:
            captured_envs.append(kwargs.get("env"))
            return await super().run(args, **kwargs)

    await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, _EnvCapturingRunner(remote_head=remote_head, local_head=local_head)),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=local_head,
        state=MonitorState(),
    )

    merge_safety_envs = [
        env for env in captured_envs if env is not None and env.get("GIT_NO_REPLACE_OBJECTS") == "1"
    ]
    assert len(merge_safety_envs) >= 5
    assert all(env.get("GIT_GRAFT_FILE") == os.devnull for env in merge_safety_envs)


@pytest.mark.unit
async def test_recovery_reset_restores_real_tree_with_replace_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_recovery_reset_lock: bool,
) -> None:
    """refs/replace on FETCH_HEAD must not survive unpublished repair recovery.

    Regression for PRRT_kwDOSJAM6s6bebd_: without GIT_NO_REPLACE_OBJECTS,
    ``git reset --hard FETCH_HEAD`` checks out the replacement tree while HEAD
    still matches the fetched SHA, so verification falsely reports success.
    """
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    monkeypatch.delenv("GIT_GRAFT_FILE", raising=False)
    monkeypatch.delenv("GIT_REPLACE_REF_BASE", raising=False)

    workspace_id = "ws_replace_recovery"
    worktree_path = tmp_path / workspace_id
    worktree_path.mkdir()
    _git(worktree_path, "init", "-q")
    _git(worktree_path, "config", "user.email", "awf@example.com")
    _git(worktree_path, "config", "user.name", "AWF Test")
    (worktree_path / "file.txt").write_text("remote\n", encoding="utf-8")
    _git(worktree_path, "add", "file.txt")
    _git(worktree_path, "commit", "-qm", "remote tip")
    remote_head = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()

    (worktree_path / "file.txt").write_text("repair\n", encoding="utf-8")
    _git(worktree_path, "add", "file.txt")
    _git(worktree_path, "commit", "-qm", "unpublished repair")
    repair_head = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()

    repair_tree = _git(worktree_path, "rev-parse", f"{repair_head}^{{tree}}").stdout.strip()
    forged = _git(
        worktree_path,
        "commit-tree",
        repair_tree,
        "-p",
        remote_head,
        "-m",
        "forged replacement",
    ).stdout.strip()
    _git(worktree_path, "update-ref", f"refs/replace/{remote_head}", forged)
    _write_fetch_head(worktree_path, remote_head)

    poisoned_reset = subprocess.run(
        ["git", "-C", str(worktree_path), "reset", "--hard", "FETCH_HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert poisoned_reset.returncode == 0
    assert (worktree_path / "file.txt").read_text(encoding="utf-8") == "repair\n"
    _git(worktree_path, "reset", "--hard", repair_head)

    async def _fetch_ok(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=AsyncioSubprocessRunner()),
        _remote_branch_fetch_once=_fetch_ok,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=repair_head,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == remote_head
    assert (worktree_path / "file.txt").read_text(encoding="utf-8") == "remote\n"


@pytest.mark.unit
async def test_behind_remote_fast_forward_rejects_graft_forged_ancestry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grafts cannot fake behind-remote ancestry to reset before provenance checks.

    Regression for PRRT_kwDOSJAM6s6beOKI: ``behind.ok`` fast-forward reset must use
    the no-replace/no-graft merge-safety env so unrelated local commits are not
    destroyed when ``info/grafts`` forges parentage.
    """
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    monkeypatch.delenv("GIT_GRAFT_FILE", raising=False)
    monkeypatch.delenv("GIT_REPLACE_REF_BASE", raising=False)

    workspace_id = "ws_graft_forgery"
    worktree_path = tmp_path / workspace_id
    repo, _ancestor, remote, lateral = _init_repo_with_lateral_and_remote(worktree_path)
    info_dir = repo / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "grafts").write_text(f"{remote} {lateral}\n", encoding="utf-8")
    _write_fetch_head(repo, remote)

    forged_check = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", lateral, remote],
        check=False,
        capture_output=True,
        text=True,
    )
    assert forged_check.returncode == 0

    async def _fetch_ok(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=AsyncioSubprocessRunner()),
        _remote_branch_fetch_once=_fetch_ok,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=lateral,
        state=MonitorState(),
    )

    assert restored_head == lateral
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == lateral


@pytest.mark.unit
async def test_abandon_rejects_mismatched_worktree_path(tmp_path: Path) -> None:
    workspace_id = "ws_mismatch"
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    (wrong / ".git").write_text("gitdir: test\n", encoding="utf-8")
    commands = _RollbackCommandRunner(remote_head="a" * 40, local_head="b" * 40)
    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=wrong,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head="b" * 40,
        state=MonitorState(),
    )
    assert restored_head == "b" * 40
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"


@pytest.mark.unit
async def test_abandon_rejects_empty_local_or_remote_heads(tmp_path: Path) -> None:
    workspace_id = "ws_empty_heads"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    commands = _RollbackCommandRunner(remote_head="a" * 40, local_head="b" * 40)
    _restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head="",
        local_head="b" * 40,
        state=MonitorState(),
    )
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"


@pytest.mark.unit
async def test_abandon_fails_when_remote_fetch_fails(tmp_path: Path) -> None:
    workspace_id = "ws_fetch_fail"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    commands = _RollbackCommandRunner(remote_head="a" * 40, local_head="b" * 40)

    async def _fetch_fail(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=1, stdout="", stderr="network down")

    runner = _runner(tmp_path, commands)
    runner._remote_branch_fetch_once = _fetch_fail
    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head="b" * 40,
        state=MonitorState(),
    )
    assert restored_head == "b" * 40
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"


@pytest.mark.unit
async def test_abandon_fails_when_fetch_head_cannot_be_resolved(tmp_path: Path) -> None:
    workspace_id = "ws_fetch_head"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")

    class _MissingFetchHeadRunner(_RollbackCommandRunner):
        async def run(self, args: list[str], **kwargs: object) -> CommandResult:
            if "rev-parse" in args and args[args.index("rev-parse") + 1] == "FETCH_HEAD":
                return CommandResult(returncode=1, stdout="", stderr="bad fetch head")
            return await super().run(args, **kwargs)

    commands = _MissingFetchHeadRunner(remote_head="a" * 40, local_head="b" * 40)
    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head="b" * 40,
        state=MonitorState(),
    )
    assert restored_head == "b" * 40
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"


@pytest.mark.unit
async def test_recovery_hard_reset_under_writer_lock_refuses_dirty_worktree(
    tmp_path: Path,
    real_recovery_reset_lock: bool,
) -> None:
    worktree_path = tmp_path / "ws_dirty_lock"
    worktree_path.mkdir()
    _git(worktree_path, "init", "-q")
    _git(worktree_path, "config", "user.email", "awf@example.com")
    _git(worktree_path, "config", "user.name", "AWF Test")
    (worktree_path / "file.txt").write_text("base\n", encoding="utf-8")
    _git(worktree_path, "add", "file.txt")
    _git(worktree_path, "commit", "-qm", "base")
    pinned_head = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
    (worktree_path / "file.txt").write_text("dirty\n", encoding="utf-8")

    result = await remote_repair_unpublished._run_recovery_hard_reset_under_writer_lock(
        AsyncioSubprocessRunner(),
        worktree_path=worktree_path,
        pinned_head=pinned_head,
        reset_target=pinned_head,
        git_env=_git_env_for_merge_safety_object_lookup(),
    )

    assert result.ready is False
    assert result.worktree_dirty is True
    assert result.reset_ok is False


@pytest.mark.unit
async def test_recovery_hard_reset_under_writer_lock_serializes_concurrent_dirty_write(
    tmp_path: Path,
    real_recovery_reset_lock: bool,
) -> None:
    worktree_path = tmp_path / "ws_writer_lock_race"
    worktree_path.mkdir()
    _git(worktree_path, "init", "-q")
    _git(worktree_path, "config", "user.email", "awf@example.com")
    _git(worktree_path, "config", "user.name", "AWF Test")
    (worktree_path / "file.txt").write_text("base\n", encoding="utf-8")
    _git(worktree_path, "add", "file.txt")
    _git(worktree_path, "commit", "-qm", "base")
    pinned_head = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
    remote_head = pinned_head

    git_env = _git_env_for_merge_safety_object_lookup()
    writer_started = threading.Event()
    writer_release = threading.Event()

    def concurrent_dirty_writer() -> None:
        with exclusive_worktree_writer_lock(worktree_path):
            writer_started.set()
            writer_release.wait()
            (worktree_path / "file.txt").write_text("racing edit\n", encoding="utf-8")

    writer = threading.Thread(target=concurrent_dirty_writer)
    writer.start()
    writer_started.wait()
    recovery_task = asyncio.create_task(
        remote_repair_unpublished._run_recovery_hard_reset_under_writer_lock(
            AsyncioSubprocessRunner(),
            worktree_path=worktree_path,
            pinned_head=pinned_head,
            reset_target=remote_head,
            git_env=git_env,
        )
    )
    writer_release.set()
    writer.join()
    recovery_result = await recovery_task

    assert recovery_result.ready is False
    assert recovery_result.worktree_dirty is True
    assert recovery_result.reset_ok is False
    assert (worktree_path / "file.txt").read_text(encoding="utf-8") == "racing edit\n"


@pytest.mark.unit
async def test_recovery_hard_reset_under_writer_lock_reports_lock_failure(
    tmp_path: Path,
    real_recovery_reset_lock: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree_path = tmp_path / "ws_lock_fail"
    worktree_path.mkdir()
    _git(worktree_path, "init", "-q")
    _git(worktree_path, "config", "user.email", "awf@example.com")
    _git(worktree_path, "config", "user.name", "AWF Test")
    (worktree_path / "file.txt").write_text("base\n", encoding="utf-8")
    _git(worktree_path, "add", "file.txt")
    _git(worktree_path, "commit", "-qm", "base")
    pinned_head = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
    lock_stderr = "permission denied creating writer lock"

    @contextlib.asynccontextmanager
    async def _raise_lock_error(_worktree_path: Path):
        raise OSError(lock_stderr)
        yield  # pragma: no cover - unreachable

    monkeypatch.setattr(
        remote_repair_unpublished,
        "hold_exclusive_worktree_writer_lock",
        _raise_lock_error,
    )

    result = await remote_repair_unpublished._run_recovery_hard_reset_under_writer_lock(
        AsyncioSubprocessRunner(),
        worktree_path=worktree_path,
        pinned_head=pinned_head,
        reset_target=pinned_head,
        git_env=_git_env_for_merge_safety_object_lookup(),
    )

    assert result.ready is False
    assert result.writer_lock_failed is True
    assert result.reset_stderr == lock_stderr


@pytest.mark.unit
async def test_recovery_hard_reset_releases_writer_lock_on_cancellation(
    tmp_path: Path,
    real_recovery_reset_lock: bool,
) -> None:
    worktree_path = tmp_path / "ws_cancel"
    worktree_path.mkdir()
    _git(worktree_path, "init", "-q")
    _git(worktree_path, "config", "user.email", "awf@example.com")
    _git(worktree_path, "config", "user.name", "AWF Test")
    (worktree_path / "file.txt").write_text("base\n", encoding="utf-8")
    _git(worktree_path, "add", "file.txt")
    _git(worktree_path, "commit", "-qm", "base")
    pinned_head = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
    git_env = _git_env_for_merge_safety_object_lookup()
    blocked = asyncio.Event()
    unblocked = asyncio.Event()

    class _BlockingRunner:
        async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
            if "rev-parse" in args:
                blocked.set()
                await unblocked.wait()
            return CommandResult(returncode=0, stdout=f"{pinned_head}\n", stderr="")

    task = asyncio.create_task(
        remote_repair_unpublished._run_recovery_hard_reset_under_writer_lock(
            _BlockingRunner(),
            worktree_path=worktree_path,
            pinned_head=pinned_head,
            reset_target=pinned_head,
            git_env=git_env,
        )
    )
    await asyncio.wait_for(blocked.wait(), timeout=2.0)
    assert is_worktree_writer_lock_held(worktree_path)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(20):
        if not is_worktree_writer_lock_held(worktree_path):
            break
        await asyncio.sleep(0.05)
    assert not is_worktree_writer_lock_held(worktree_path)


@pytest.mark.unit
async def test_behind_remote_fast_forward_reports_writer_lock_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_behind_lock_fail"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "c" * 40
    stale_local_head = "a" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=stale_local_head,
        local_behind_remote=True,
    )
    lock_stderr = "permission denied creating writer lock"

    async def _lock_failed(
        *_args: object,
        **_kwargs: object,
    ) -> remote_repair_unpublished._RecoveryResetOutcome:
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=False,
            live_head=None,
            worktree_dirty=False,
            reset_ok=False,
            reset_stderr=lock_stderr,
            writer_lock_failed=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _lock_failed,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=stale_local_head,
        state=MonitorState(),
    )

    assert restored_head == stale_local_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"
    assert result.details["reset_stderr"] == lock_stderr
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_unpublished_reset_reports_writer_lock_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_unpublished_lock_fail"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    abandoned_head = "b" * 40
    commands = _RollbackCommandRunner(remote_head=remote_head, local_head=abandoned_head)
    lock_stderr = "permission denied creating writer lock"

    async def _lock_failed(
        *_args: object,
        **_kwargs: object,
    ) -> remote_repair_unpublished._RecoveryResetOutcome:
        return remote_repair_unpublished._RecoveryResetOutcome(
            ready=False,
            live_head=None,
            worktree_dirty=False,
            reset_ok=False,
            reset_stderr=lock_stderr,
            writer_lock_failed=True,
        )

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_run_recovery_hard_reset_under_writer_lock",
        _lock_failed,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=abandoned_head,
        state=MonitorState(),
    )

    assert restored_head == abandoned_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_ROLLBACK_FAILED"
    assert result.details["reset_stderr"] == lock_stderr
    assert all("reset" not in call for call in commands.calls)
