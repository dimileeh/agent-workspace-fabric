"""Post-restart recovery preserves accepted comment-repair commits (#935, part 001).

A control-plane restart mid ``AddressComments`` batch leaves accepted item commits
local and unpushed. Recovery must resume the batch when those commits are provably
AWF repair work, and otherwise park the workspace for a human with the worktree
intact — never terminally fail it, never reset unknown commits.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_unpublished

_REMOTE_HEAD = "a" * 40
_ADVANCED_REMOTE_HEAD = "f" * 40
_LOCAL_HEAD = "b" * 40


class _RecoveryCommandRunner:
    """Command runner that models a worktree ahead of the fetched PR head."""

    def __init__(
        self,
        *,
        remote_head: str,
        local_head: str,
        ancestry: dict[tuple[str, str], bool] | None = None,
        log_stdout: str = "",
        log_ok: bool = True,
    ) -> None:
        self.remote_head = remote_head
        self.local_head = local_head
        self.ancestry = ancestry
        self.log_stdout = log_stdout
        self.log_ok = log_ok
        self.calls: list[tuple[str, ...]] = []

    def _resolve(self, ref: str) -> str:
        if ref == "FETCH_HEAD":
            return self.remote_head
        if ref == "HEAD":
            return self.local_head
        return ref

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
            return CommandResult(
                returncode=0,
                stdout=f"{self._resolve(call[call.index('rev-parse') + 1])}\n",
                stderr="",
            )
        if "merge-base" in call and "--is-ancestor" in call:
            index = call.index("--is-ancestor")
            ok = self._is_ancestor(self._resolve(call[index + 1]), self._resolve(call[index + 2]))
            return CommandResult(returncode=0 if ok else 1, stdout="", stderr="")
        if "log" in call:
            return CommandResult(
                returncode=0 if self.log_ok else 128,
                stdout=self.log_stdout,
                stderr="" if self.log_ok else "fatal: bad revision",
            )
        if "diff" in call:
            return CommandResult(returncode=0, stdout="M\0src/example.py\0", stderr="")
        if "reset" in call:
            self.local_head = self.remote_head
            return CommandResult(returncode=0, stdout="", stderr="")
        if "status" in call:
            return CommandResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {call}")


class _RecoveryRunner(SimpleNamespace):
    """Runner seam recording the operator-facing side effects of recovery."""


def _runner(tmp_path: Path, commands: _RecoveryCommandRunner) -> _RecoveryRunner:
    recorded: list[object] = []

    async def _fetch(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    async def _append_events(*, workspace_id: str, events: list[object]) -> None:
        del workspace_id
        recorded.extend(events)

    return _RecoveryRunner(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=commands),
        _remote_branch_fetch_once=_fetch,
        _append_workspace_events=_append_events,
        appended_events=recorded,
    )


def _worktree(tmp_path: Path, workspace_id: str) -> Path:
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: test\n", encoding="utf-8")
    return worktree


def _chain_state(records: list[dict[str, object]]) -> MonitorState:
    state = MonitorState()
    state.mark_addressed(
        _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY,
        json.dumps(records, separators=(",", ":"), sort_keys=True),
    )
    return state


@pytest.fixture(autouse=True)
def _verified_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_verified_awf_comment_repair_worktree",
        lambda **_kwargs: True,
    )

    async def _ownership_ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_repair_unpublished, "repair_agent_runtime_ownership", _ownership_ok)


def _patch_operation_provenance(
    monkeypatch: pytest.MonkeyPatch,
    *,
    comment_repair: bool,
    conflicting: bool,
) -> None:
    async def _comment(*_args: object, **_kwargs: object) -> bool:
        return comment_repair

    async def _conflicting(*_args: object, **_kwargs: object) -> bool:
        return conflicting

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_comment_repair_has_operation_provenance",
        _comment,
    )
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_non_comment_repair_has_operation_provenance",
        _conflicting,
    )


@pytest.mark.unit
async def test_restart_mid_batch_with_item_chain_resumes_the_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_chain_resume"
    _worktree(tmp_path, workspace_id)
    commands = _RecoveryCommandRunner(remote_head=_REMOTE_HEAD, local_head=_LOCAL_HEAD)
    runner = _runner(tmp_path, commands)
    _patch_operation_provenance(monkeypatch, comment_repair=False, conflicting=False)
    state = _chain_state(
        [
            {
                "item_id": "PRRT_kwDOSJAM6s6fjOze",
                "item_start_head": _REMOTE_HEAD,
                "head_sha": _LOCAL_HEAD,
                "operation_id": "op_comment_repair",
            }
        ]
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        state=state,
        current_operation_id="op_comment_repair",
    )

    assert result is None
    assert restored_head == _LOCAL_HEAD
    assert commands.local_head == _LOCAL_HEAD
    assert all("reset" not in call for call in commands.calls)
    assert [event.reason_code for event in runner.appended_events] == [
        "COMMENT_REPAIR_UNPUBLISHED_PRESERVED"
    ]


@pytest.mark.unit
async def test_legacy_review_item_subjects_resume_the_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_legacy_resume"
    _worktree(tmp_path, workspace_id)
    commands = _RecoveryCommandRunner(
        remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        log_stdout=(
            "3195fc8 fix: address PR review thread PRRT_kwDOSJAM6s6fjOze\n"
            "aa194c9 test: cover the new guard\n"
        ),
    )
    runner = _runner(tmp_path, commands)
    _patch_operation_provenance(monkeypatch, comment_repair=False, conflicting=False)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        state=MonitorState(),
        current_operation_id="op_comment_repair",
    )

    assert result is None
    assert restored_head == _LOCAL_HEAD
    assert all("reset" not in call for call in commands.calls)
    assert [event.reason_code for event in runner.appended_events] == [
        "COMMENT_REPAIR_UNPUBLISHED_PRESERVED"
    ]


@pytest.mark.unit
async def test_unknown_local_commits_park_for_a_human_without_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_park"
    _worktree(tmp_path, workspace_id)
    commands = _RecoveryCommandRunner(
        remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        log_stdout="3195fc8 chore: unrelated local work\n",
    )
    runner = _runner(tmp_path, commands)
    _patch_operation_provenance(monkeypatch, comment_repair=False, conflicting=False)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        state=MonitorState(),
        current_operation_id="op_comment_repair",
    )

    assert restored_head == _LOCAL_HEAD
    assert commands.local_head == _LOCAL_HEAD
    assert all("reset" not in call and "checkout" not in call for call in commands.calls)
    assert result is not None
    assert result.failed is True
    assert result.parked_needs_human is True
    assert result.terminal_monitor_failure is False
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING"
    # The park reason names the preserved commits so the operator can inspect them.
    assert "3195fc8" in result.stderr
    assert "chore: unrelated local work" in result.stderr
    assert [event.reason_code for event in runner.appended_events] == [
        "COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING"
    ]


@pytest.mark.unit
async def test_unreadable_commit_log_parks_instead_of_preserving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_park_log_failed"
    _worktree(tmp_path, workspace_id)
    commands = _RecoveryCommandRunner(
        remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        log_ok=False,
    )
    runner = _runner(tmp_path, commands)
    _patch_operation_provenance(monkeypatch, comment_repair=False, conflicting=False)

    _restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        state=MonitorState(),
        current_operation_id="op_comment_repair",
    )

    assert result is not None
    assert result.parked_needs_human is True
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_stale_snapshot_advance_with_chain_parks_instead_of_preserving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remote advanced past the batch base, so the preserved commits could not
    fast-forward. Park rather than resume a push that cannot land."""
    workspace_id = "ws_stale_snapshot"
    _worktree(tmp_path, workspace_id)
    commands = _RecoveryCommandRunner(
        remote_head=_ADVANCED_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        ancestry={
            (_REMOTE_HEAD, _ADVANCED_REMOTE_HEAD): True,
            (_ADVANCED_REMOTE_HEAD, _LOCAL_HEAD): False,
            (_LOCAL_HEAD, _ADVANCED_REMOTE_HEAD): False,
            (_REMOTE_HEAD, _LOCAL_HEAD): True,
        },
        log_stdout="3195fc8 fix: address PR review thread PRRT_kwDOSJAM6s6fjOze\n",
    )
    runner = _runner(tmp_path, commands)
    _patch_operation_provenance(monkeypatch, comment_repair=False, conflicting=False)
    state = _chain_state(
        [
            {
                "item_id": "PRRT_kwDOSJAM6s6fjOze",
                "item_start_head": _REMOTE_HEAD,
                "head_sha": _LOCAL_HEAD,
                "operation_id": "op_comment_repair",
            }
        ]
    )

    _restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        state=state,
        current_operation_id="op_comment_repair",
    )

    assert result is not None
    assert result.parked_needs_human is True
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING"
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_conflicting_repair_provenance_parks_and_never_resets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_conflicting"
    _worktree(tmp_path, workspace_id)
    commands = _RecoveryCommandRunner(
        remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        log_stdout="3195fc8 fix: address PR review thread PRRT_kwDOSJAM6s6fjOze\n",
    )
    runner = _runner(tmp_path, commands)
    _patch_operation_provenance(monkeypatch, comment_repair=True, conflicting=True)
    state = _chain_state(
        [
            {
                "item_id": "PRRT_kwDOSJAM6s6fjOze",
                "item_start_head": _REMOTE_HEAD,
                "head_sha": _LOCAL_HEAD,
                "operation_id": "op_comment_repair",
            }
        ]
    )

    _restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        state=state,
        current_operation_id="op_comment_repair",
    )

    assert result is not None
    assert result.parked_needs_human is True
    assert commands.local_head == _LOCAL_HEAD
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_operation_level_provenance_still_resets_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: an operation-owned interrupted repair is still abandoned."""
    workspace_id = "ws_operation_reset"
    _worktree(tmp_path, workspace_id)
    commands = _RecoveryCommandRunner(remote_head=_REMOTE_HEAD, local_head=_LOCAL_HEAD)
    runner = _runner(tmp_path, commands)
    runner._persist_state = _noop_persist_state
    _patch_operation_provenance(monkeypatch, comment_repair=True, conflicting=False)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=_REMOTE_HEAD,
        local_head=_LOCAL_HEAD,
        state=MonitorState(),
        current_operation_id="op_comment_repair",
    )

    assert result is None
    assert restored_head == _REMOTE_HEAD
    assert any("reset" in call for call in commands.calls)


async def _noop_persist_state(_workspace_id: str, _state: MonitorState) -> None:
    return None
