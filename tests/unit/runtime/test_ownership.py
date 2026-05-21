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

    captured: list[tuple[Path | None, Path, Path | None]] = []

    def _repair_agent_writable_worktree(
        layout_mirror: Path | None, path: Path, linked_git_dir: Path | None = None
    ) -> None:
        captured.append((layout_mirror, path, linked_git_dir))

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
    assert captured == [(mirror_root / "repo.git", worktree_path, linked_git_dir)]


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_passes_validated_git_metadata(
    tmp_path: Path,
) -> None:
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

    linked_read_calls = 0

    def _single_read_linked_git_dir(_path: Path) -> Path | None:
        nonlocal linked_read_calls
        linked_read_calls += 1
        if linked_read_calls > 1:
            raise AssertionError("linked .git metadata was re-read during repair")
        return linked_git_dir

    captured: list[tuple[Path | None, Path, Path | None]] = []

    def _repair_agent_writable_worktree(
        layout_mirror: Path | None, path: Path, linked_git_dir: Path | None = None
    ) -> None:
        captured.append((layout_mirror, path, linked_git_dir))

    logger = _RecordingLogger()
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(ownership, "_linked_worktree_git_dir", _single_read_linked_git_dir)
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
    assert linked_read_calls == 1
    assert captured == [(mirror_root / "repo.git", worktree_path, linked_git_dir)]


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_allows_numeric_worktree_suffix(
    tmp_path: Path,
) -> None:
    workspace_id = "ws"
    worktrees_root = tmp_path / "workspace"
    mirror_root = worktrees_root.parent / "mirrors"
    worktree_path = worktrees_root / workspace_id
    worktree_path.mkdir(parents=True)
    linked_git_dir = mirror_root / "repo.git" / "worktrees" / f"{workspace_id}1"
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )

    captured: list[tuple[Path | None, Path, Path | None]] = []

    def _repair_agent_writable_worktree(
        layout_mirror: Path | None, path: Path, linked_git_dir: Path | None = None
    ) -> None:
        captured.append((layout_mirror, path, linked_git_dir))

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
    assert captured == [(mirror_root / "repo.git", worktree_path, linked_git_dir)]


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

    def _repair_agent_writable_worktree(
        _layout_mirror: Path | None, _path: Path, _linked_git_dir: Path | None = None
    ) -> None:
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


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_blocks_wrong_workspace_mirror(
    tmp_path: Path,
) -> None:
    workspace_id = "ws"
    bad_workspace_id = "other"
    worktrees_root = tmp_path / "workspace"
    mirror_root = worktrees_root.parent / "mirrors"
    worktree_path = worktrees_root / workspace_id
    worktree_path.mkdir(parents=True)
    linked_git_dir = mirror_root / "repo.git" / "worktrees" / bad_workspace_id
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )

    called = False

    def _repair_agent_writable_worktree(
        _layout_mirror: Path | None, _path: Path, _linked_git_dir: Path | None = None
    ) -> None:
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


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_blocks_numeric_worktree_without_workspace_prefix(
    tmp_path: Path,
) -> None:
    workspace_id = "ws"
    worktrees_root = tmp_path / "workspace"
    mirror_root = worktrees_root.parent / "mirrors"
    worktree_path = worktrees_root / workspace_id
    worktree_path.mkdir(parents=True)
    linked_git_dir = mirror_root / "repo.git" / "worktrees" / "123"
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )

    called = False

    def _repair_agent_writable_worktree(
        _layout_mirror: Path | None, _path: Path, _linked_git_dir: Path | None = None
    ) -> None:
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
