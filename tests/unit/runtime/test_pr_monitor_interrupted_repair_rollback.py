"""Recovery of unpublished comment-repair commits without output salvage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_unpublished


class _RollbackCommandRunner:
    def __init__(
        self,
        *,
        remote_head: str,
        local_head: str,
        local_behind_remote: bool = False,
    ) -> None:
        self.remote_head = remote_head
        self.local_head = local_head
        self.local_behind_remote = local_behind_remote
        self.calls: list[tuple[str, ...]] = []

    async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if "rev-parse" in call:
            ref = call[call.index("rev-parse") + 1]
            head = self.remote_head if ref == "FETCH_HEAD" else self.local_head
            return CommandResult(returncode=0, stdout=f"{head}\n", stderr="")
        if "merge-base" in call and "--is-ancestor" in call:
            ancestor = call[call.index("--is-ancestor") + 1]
            descendant = call[call.index("--is-ancestor") + 2]
            if self.local_behind_remote:
                if ancestor == "FETCH_HEAD" and descendant == "HEAD":
                    return CommandResult(returncode=1, stdout="", stderr="")
                if ancestor == "HEAD" and descendant == "FETCH_HEAD":
                    return CommandResult(returncode=0, stdout="", stderr="")
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
    assert reset_calls[0][-2:] == ("--hard", "FETCH_HEAD")
    assert all("diff" not in call for call in commands.calls)


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
        "merge-base" in call and call[-3:] == ("--is-ancestor", stale_snapshot, "FETCH_HEAD")
        for call in commands.calls
    )
    assert all("reset" not in call for call in commands.calls)


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

    assert result is None
    assert restored_head == ci_repair_head
    assert commands.local_head == ci_repair_head
    assert all("reset" not in call for call in commands.calls)


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
