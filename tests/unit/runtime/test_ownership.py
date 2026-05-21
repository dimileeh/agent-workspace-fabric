"""Regression tests for runtime ownership repair safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime import ownership


class _RecordingLogger:
    def __init__(self) -> None:
        self.exception_calls: list[tuple[str, dict[str, object]]] = []

    def exception(
        self,
        event: str,
        *,
        workspace_id: str,
        worktree_path: str,
        reason: str,
        reason_code: str,
    ) -> None:
        self.exception_calls.append(
            (
                event,
                {
                    "workspace_id": workspace_id,
                    "worktree_path": worktree_path,
                    "reason": reason,
                    "reason_code": reason_code,
                },
            )
        )


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_uses_mirror_from_worktree(tmp_path: Path) -> None:
    workspace_id = "ws"
    worktrees_root = tmp_path / "workspace"
    mirror_root = worktrees_root.parent / "mirrors"
    worktree_path = worktrees_root / workspace_id
    worktree_path.mkdir(parents=True)
    linked_git_dir = mirror_root / "repo.git" / "worktrees" / workspace_id
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )

    captured: list[tuple[Path | None, Path]] = []

    def _repair_agent_writable_worktree(layout_mirror: Path | None, path: Path) -> None:
        captured.append((layout_mirror, path))

    logger = _RecordingLogger()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(
        ownership,
        "repair_agent_writable_worktree",
        _repair_agent_writable_worktree,
    )

    try:
        ok = await ownership.repair_agent_runtime_ownership(
            logger=logger,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="pytest",
            event_name="monitor.event",
            reason_code="AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED",
        )
    finally:
        monkeypatched.undo()

    assert ok
    assert captured == [(mirror_root / "repo.git", worktree_path)]


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_blocks_mirrors_outside_workspace_mirrors_root(
    tmp_path: Path,
) -> None:
    workspace_id = "ws"
    worktrees_root = tmp_path / "workspace"
    worktree_path = worktrees_root / workspace_id
    worktree_path.mkdir(parents=True)
    malicious_git_dir = tmp_path / "outside" / "repo.git" / "worktrees" / workspace_id
    malicious_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text(
        f"gitdir: {malicious_git_dir}\n",
        encoding="utf-8",
    )

    called = False

    def _repair_agent_writable_worktree(_layout_mirror: Path | None, _path: Path) -> None:
        nonlocal called
        called = True

    logger = _RecordingLogger()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(
        ownership,
        "repair_agent_writable_worktree",
        _repair_agent_writable_worktree,
    )

    try:
        ok = await ownership.repair_agent_runtime_ownership(
            logger=logger,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="pytest",
            event_name="monitor.event",
            reason_code="AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED",
        )
    finally:
        monkeypatched.undo()

    assert ok is False
    assert called is False
    assert len(logger.exception_calls) == 1
    assert logger.exception_calls[0][0] == "monitor.event"
