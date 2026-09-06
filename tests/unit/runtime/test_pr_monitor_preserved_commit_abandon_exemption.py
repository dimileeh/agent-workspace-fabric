"""A correction-preserved commit is exempt from unpublished-repair abandonment.

The #925 correction outcomes escalate to ``needs_human`` while deliberately
keeping the agent's commit, and a failed push requeues that item so the next
cycle retries publishing it. The same failure records the unpushed HEAD as
abandon provenance — which is exactly what would let
``_abandon_unpublished_comment_repairs`` hard-reset the commit preserved for
human review before the re-address. The recorded head is therefore exempt from
that reset, and only that head (PRRT_kwDOSJAM6s6fqJVM).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_unpublished
from awf.runtime.pr_monitor_runner.helpers import (
    _retain_preserved_unpublished_commit_head,
)

_REMOTE_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40


class _RefusingCommandRunner:
    """Command runner that fails the test if recovery touches the worktree."""

    async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
        raise AssertionError(f"unexpected command: {tuple(args)}")


def _runner(tmp_path: Path) -> SimpleNamespace:
    async def _fetch(**_kwargs: object) -> CommandResult:
        raise AssertionError("unexpected remote fetch")

    return SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=_RefusingCommandRunner()),
        _remote_branch_fetch_once=_fetch,
    )


def _worktree(tmp_path: Path, workspace_id: str) -> Path:
    worktree_path = tmp_path / workspace_id
    worktree_path.mkdir()
    (worktree_path / ".git").write_text("gitdir: test\n", encoding="utf-8")
    return worktree_path


@pytest.mark.unit
async def test_retained_preserved_commit_head_is_never_reset(tmp_path: Path) -> None:
    """The requeue retries publishing the preserved commit instead of deleting it."""
    workspace_id = "ws_preserved_correction"
    worktree_path = _worktree(tmp_path, workspace_id)
    state = MonitorState()
    # Case-insensitive: the recorded provenance and ``git rev-parse`` output can
    # differ in case without describing different commits.
    _retain_preserved_unpublished_commit_head(state, _PRESERVED_HEAD.upper())

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path),
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch="fix/review",
        expected_remote_head=_REMOTE_HEAD,
        local_head=_PRESERVED_HEAD,
        state=state,
    )

    assert result is None
    assert restored_head == _PRESERVED_HEAD


@pytest.mark.unit
async def test_retained_head_for_another_commit_does_not_exempt_local_history(
    tmp_path: Path,
) -> None:
    """Only the recorded SHA is exempt; other local-ahead history still fails closed."""
    workspace_id = "ws_preserved_stale"
    worktree_path = _worktree(tmp_path, workspace_id)
    state = MonitorState()
    _retain_preserved_unpublished_commit_head(state, "d" * 40)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path),
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch="fix/review",
        expected_remote_head=_REMOTE_HEAD,
        local_head=_PRESERVED_HEAD,
        state=state,
    )

    # Recovery proceeded past the exemption and fell closed on the unverifiable
    # (fixture-only) Git layout rather than short-circuiting as exempt.
    assert restored_head == _PRESERVED_HEAD
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"


@pytest.mark.unit
async def test_no_retained_head_leaves_recovery_unchanged(tmp_path: Path) -> None:
    """Without a preserved commit the ordinary abandon contract is untouched."""
    workspace_id = "ws_no_preserved"
    worktree_path = _worktree(tmp_path, workspace_id)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path),
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch="fix/review",
        expected_remote_head=_REMOTE_HEAD,
        local_head=_PRESERVED_HEAD,
        state=MonitorState(),
    )

    assert restored_head == _PRESERVED_HEAD
    assert result is not None
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
