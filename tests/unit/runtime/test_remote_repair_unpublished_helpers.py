"""Unit tests for remote unpublished-repair helper functions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.runtime.pr_monitor_runner import remote_repair_unpublished


@pytest.mark.unit
def test_verified_awf_comment_repair_worktree_rejects_mismatched_layout(tmp_path: Path) -> None:
    workspace_id = "ws_layout"
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: missing\n", encoding="utf-8")
    runner = SimpleNamespace(_worktrees_root=tmp_path)
    assert (
        remote_repair_unpublished._verified_awf_comment_repair_worktree(
            runner=runner,
            workspace_id=workspace_id,
            worktree_path=tmp_path / "other",
        )
        is False
    )
