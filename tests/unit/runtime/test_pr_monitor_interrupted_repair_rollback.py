"""Recovery of unpublished comment-repair commits without output salvage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_unpublished


class _RollbackCommandRunner:
    def __init__(self, *, remote_head: str, local_head: str) -> None:
        self.remote_head = remote_head
        self.local_head = local_head
        self.calls: list[tuple[str, ...]] = []

    async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if "rev-parse" in call:
            ref = call[call.index("rev-parse") + 1]
            head = self.remote_head if ref == "FETCH_HEAD" else self.local_head
            return CommandResult(returncode=0, stdout=f"{head}\n", stderr="")
        if "merge-base" in call and "--is-ancestor" in call:
            return CommandResult(returncode=0, stdout="", stderr="")
        if "diff" in call:
            return CommandResult(returncode=0, stdout="M\0src/example.py\0", stderr="")
        if "reset" in call:
            self.local_head = self.remote_head
            return CommandResult(returncode=0, stdout="", stderr="")
        if "status" in call:
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
    assert reset_calls[0][-2:] == ("--hard", "FETCH_HEAD")


@pytest.mark.unit
async def test_remote_head_mismatch_fails_without_reset(tmp_path: Path) -> None:
    workspace_id = "ws_mismatch"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    expected = "a" * 40
    fetched = "c" * 40
    commands = _RollbackCommandRunner(remote_head=fetched, local_head="b" * 40)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=expected,
        local_head="b" * 40,
        state=MonitorState(),
    )

    assert restored_head == "b" * 40
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
