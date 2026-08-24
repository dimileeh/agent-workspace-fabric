"""Regression tests for runtime ownership repair safety."""

from __future__ import annotations

from collections.abc import Callable
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


@pytest.fixture(autouse=True)
def _run_ownership_repair_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ownership.os, "geteuid", lambda: 0)


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_noops_when_not_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws"
    worktree_path = tmp_path / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    called = False

    def _repair_agent_writable_worktree(
        _layout_mirror: Path | None,
        _path: Path,
        linked_git_dir: Path | None = None,
    ) -> None:
        _ = linked_git_dir
        nonlocal called
        called = True

    logger = _RecordingLogger()
    monkeypatch.setattr(ownership.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        ownership,
        "repair_agent_writable_worktree",
        _repair_agent_writable_worktree,
    )

    ok = await ownership.repair_agent_runtime_ownership(
        logger=logger,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="pytest",
        event_name="monitor.event",
        reason_code="AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED",
    )

    assert ok is True
    assert called is False
    assert logger.exception_calls == []


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
    monkeypatched.setattr(ownership, "linked_worktree_git_dir", _single_read_linked_git_dir)
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
async def test_repair_agent_runtime_ownership_runs_validation_inside_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws"
    worktree_path = tmp_path / "worktrees" / workspace_id
    mirror_path = tmp_path / "mirrors" / "repo.git"
    linked_git_dir = mirror_path / "worktrees" / workspace_id
    inside_to_thread = False
    calls: list[tuple[str, bool]] = []

    async def _to_thread(
        func: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal inside_to_thread
        inside_to_thread = True
        try:
            return func(*args, **kwargs)
        finally:
            inside_to_thread = False

    def _validated_layout_mirror_for_worktree(
        path: Path, validated_workspace_id: str
    ) -> tuple[Path, Path]:
        calls.append(("validate", inside_to_thread))
        assert path == worktree_path
        assert validated_workspace_id == workspace_id
        return mirror_path, linked_git_dir

    def _repair_agent_writable_worktree(
        layout_mirror: Path | None,
        path: Path,
        **kwargs: object,
    ) -> None:
        calls.append(("repair", inside_to_thread))
        assert layout_mirror == mirror_path
        assert path == worktree_path
        assert kwargs == {"linked_git_dir": linked_git_dir}

    logger = _RecordingLogger()
    monkeypatch.setattr(ownership.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(
        ownership,
        "_validated_layout_mirror_for_worktree",
        _validated_layout_mirror_for_worktree,
    )
    monkeypatch.setattr(
        ownership,
        "repair_agent_writable_worktree",
        _repair_agent_writable_worktree,
    )

    ok = await ownership.repair_agent_runtime_ownership(
        logger=logger,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="pytest",
        event_name="monitor.event",
        reason_code="AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED",
    )

    assert ok
    assert logger.exception_calls == []
    assert calls == [("validate", True), ("repair", True)]


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_blocks_divergent_git_metadata_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws"
    worktrees_root = tmp_path / "workspace"
    worktree_path = worktrees_root / workspace_id
    worktree_path.mkdir(parents=True)
    malicious_git_dir = tmp_path / "outside" / "repo.git" / "worktrees" / workspace_id
    malicious_git_dir.mkdir(parents=True)
    git_file = worktree_path / ".git"
    git_file.write_text("not-a-gitdir\n", encoding="utf-8")

    original_read_text = Path.read_text
    git_file_reads = 0

    def _divergent_read_text(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal git_file_reads
        if path == git_file:
            git_file_reads += 1
            if git_file_reads == 1:
                return "not-a-gitdir\n"
            return f"gitdir: {malicious_git_dir}\n"
        return original_read_text(path, *args, **kwargs)

    called = False

    def _repair_agent_writable_worktree(
        _layout_mirror: Path | None,
        _path: Path,
        linked_git_dir: Path | None = None,
    ) -> None:
        _ = linked_git_dir
        nonlocal called
        called = True

    logger = _RecordingLogger()
    monkeypatch.setattr(Path, "read_text", _divergent_read_text)
    monkeypatch.setattr(
        ownership,
        "repair_agent_writable_worktree",
        _repair_agent_writable_worktree,
    )

    ok = await ownership.repair_agent_runtime_ownership(
        logger=logger,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="pytest",
        event_name="monitor.event",
        reason_code="AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED",
    )

    assert ok is False
    assert called is False
    assert git_file_reads == 1
    assert len(logger.exception_calls) == 1
    assert logger.exception_calls[0][0] == "monitor.event"


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_allows_symlinked_mirror_prefix(
    tmp_path: Path,
) -> None:
    workspace_id = "ws"
    real_work_dir = tmp_path / "real-work-dir"
    real_work_dir.mkdir()
    linked_work_dir = tmp_path / "linked-work-dir"
    linked_work_dir.symlink_to(real_work_dir, target_is_directory=True)

    worktrees_root = linked_work_dir / "worktrees"
    mirror_root = linked_work_dir / "mirrors"
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
    assert logger.exception_calls == []
    assert captured == [
        ((real_work_dir / "mirrors" / "repo.git").resolve(), worktree_path, linked_git_dir)
    ]


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_allows_symlinked_mirror_worktrees_dir(
    tmp_path: Path,
) -> None:
    workspace_id = "ws"
    worktrees_root = tmp_path / "workspace"
    mirror_root = worktrees_root.parent / "mirrors"
    worktree_path = worktrees_root / workspace_id
    worktree_path.mkdir(parents=True)

    mirror_path = mirror_root / "repo.git"
    mirror_path.mkdir(parents=True)
    real_mirror_worktrees = tmp_path / "real-mirror-worktrees"
    real_mirror_worktrees.mkdir()
    (mirror_path / "worktrees").symlink_to(real_mirror_worktrees, target_is_directory=True)

    linked_git_dir = mirror_path / "worktrees" / workspace_id
    linked_git_dir.mkdir()
    (linked_git_dir / "commondir").write_text(
        f"{mirror_path}\n",
        encoding="utf-8",
    )
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
    assert logger.exception_calls == []
    assert captured == [(mirror_path, worktree_path, linked_git_dir)]


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_blocks_workspace_id_prefix_collision(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_1"
    worktrees_root = tmp_path / "workspace"
    mirror_root = worktrees_root.parent / "mirrors"
    worktree_path = worktrees_root / workspace_id
    worktree_path.mkdir(parents=True)
    linked_git_dir = mirror_root / "repo.git" / "worktrees" / "ws_12"
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )

    called = False

    def _repair_agent_writable_worktree(
        _layout_mirror: Path | None,
        _path: Path,
        linked_git_dir: Path | None = None,
    ) -> None:
        _ = linked_git_dir
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
def test_mirror_path_from_linked_git_dir_uses_fallback_without_commondir(tmp_path: Path) -> None:
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)

    assert (
        ownership._mirror_path_from_linked_git_dir(linked_git_dir)
        == (  # noqa: SLF001
            tmp_path / "mirrors" / "repo.git"
        ).resolve()
    )


@pytest.mark.unit
def test_mirror_path_from_linked_git_dir_resolves_relative_commondir(tmp_path: Path) -> None:
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    (linked_git_dir / "commondir").write_text("../..", encoding="utf-8")

    assert (
        ownership._mirror_path_from_linked_git_dir(linked_git_dir)
        == (  # noqa: SLF001
            tmp_path / "mirrors" / "repo.git"
        ).resolve()
    )


@pytest.mark.unit
def test_mirror_path_from_linked_git_dir_handles_empty_and_absolute_commondir(
    tmp_path: Path,
) -> None:
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    commondir = linked_git_dir / "commondir"

    commondir.write_text("", encoding="utf-8")
    assert (
        ownership._mirror_path_from_linked_git_dir(linked_git_dir)
        == (  # noqa: SLF001
            tmp_path / "mirrors" / "repo.git"
        ).resolve()
    )

    absolute_common = tmp_path / "absolute-mirror"
    absolute_common.mkdir()
    commondir.write_text(str(absolute_common), encoding="utf-8")
    assert ownership._mirror_path_from_linked_git_dir(linked_git_dir) == (  # noqa: SLF001
        absolute_common.resolve()
    )


@pytest.mark.unit
def test_mirror_path_from_linked_git_dir_reports_unreadable_commondir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    (linked_git_dir / "commondir").write_text("../..", encoding="utf-8")
    original_read_text = Path.read_text

    def _read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == linked_git_dir / "commondir":
            raise OSError("nope")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)

    with pytest.raises(ValueError, match="cannot resolve mirror path"):
        ownership._mirror_path_from_linked_git_dir(linked_git_dir)  # noqa: SLF001


@pytest.mark.unit
def test_mirror_path_from_linked_git_dir_reports_unresolvable_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws"
    linked_git_dir.mkdir(parents=True)
    original_resolve = Path.resolve

    def _resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == linked_git_dir.parent.parent:
            raise RuntimeError("symlink loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)

    with pytest.raises(ValueError, match="cannot resolve mirror path"):
        ownership._mirror_path_from_linked_git_dir(linked_git_dir)  # noqa: SLF001


@pytest.mark.unit
def test_validate_linked_git_dir_backref_rejects_missing_workspace_git_file(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktrees" / "ws"
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws1"
    worktree_path.mkdir(parents=True)
    linked_git_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="non-symlink file"):
        ownership._validate_linked_git_dir_backref(linked_git_dir, worktree_path)  # noqa: SLF001


@pytest.mark.unit
def test_validate_linked_git_dir_backref_accepts_relative_gitdir(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktrees" / "ws"
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws1"
    worktree_path.mkdir(parents=True)
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text("gitdir: mirror\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(
        str(Path("..") / ".." / ".." / ".." / "worktrees" / "ws" / ".git"),
        encoding="utf-8",
    )

    ownership._validate_linked_git_dir_backref(linked_git_dir, worktree_path)  # noqa: SLF001


@pytest.mark.unit
def test_validate_linked_git_dir_backref_rejects_empty_gitdir(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktrees" / "ws"
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws1"
    worktree_path.mkdir(parents=True)
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text("gitdir: mirror\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        ownership._validate_linked_git_dir_backref(linked_git_dir, worktree_path)  # noqa: SLF001


@pytest.mark.unit
def test_validate_linked_git_dir_backref_reports_unreadable_gitdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktrees" / "ws"
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws1"
    worktree_path.mkdir(parents=True)
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text("gitdir: mirror\n", encoding="utf-8")
    metadata_gitdir = linked_git_dir / "gitdir"
    metadata_gitdir.write_text("relative/.git", encoding="utf-8")
    original_read_text = Path.read_text

    def _read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == metadata_gitdir:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)

    with pytest.raises(ValueError, match="cannot read linked-worktree metadata"):
        ownership._validate_linked_git_dir_backref(linked_git_dir, worktree_path)  # noqa: SLF001


@pytest.mark.unit
def test_validate_linked_git_dir_backref_reports_unresolvable_gitdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktrees" / "ws"
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws"
    worktree_path.mkdir(parents=True)
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text("gitdir: mirror\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text("relative/.git", encoding="utf-8")

    original_resolve = Path.resolve

    def _resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == linked_git_dir / "relative" / ".git":
            raise RuntimeError("loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)

    with pytest.raises(ValueError, match="cannot resolve linked-worktree metadata"):
        ownership._validate_linked_git_dir_backref(linked_git_dir, worktree_path)  # noqa: SLF001


@pytest.mark.unit
def test_validate_linked_git_dir_backref_rejects_wrong_backref(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktrees" / "ws"
    other_worktree = tmp_path / "worktrees" / "other"
    linked_git_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws1"
    worktree_path.mkdir(parents=True)
    other_worktree.mkdir(parents=True)
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text("gitdir: mirror\n", encoding="utf-8")
    (other_worktree / ".git").write_text("gitdir: mirror\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(str(other_worktree / ".git"), encoding="utf-8")

    with pytest.raises(ValueError, match="points to another workspace"):
        ownership._validate_linked_git_dir_backref(linked_git_dir, worktree_path)  # noqa: SLF001


@pytest.mark.unit
def test_validated_layout_mirror_rejects_missing_linked_git_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktrees" / "ws"
    worktree_path.mkdir(parents=True)
    monkeypatch.setattr(ownership, "linked_worktree_git_dir", lambda _path: None)

    with pytest.raises(ValueError, match="cannot read linked-worktree git metadata"):
        ownership._validated_layout_mirror_for_worktree(worktree_path, "ws")  # noqa: SLF001


@pytest.mark.unit
def test_validated_layout_mirror_rejects_wrong_metadata_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktrees" / "ws"
    mirror_path = tmp_path / "mirrors" / "repo.git"
    linked_git_dir = mirror_path / "wrong-parent" / "ws"
    worktree_path.mkdir(parents=True)
    linked_git_dir.mkdir(parents=True)

    monkeypatch.setattr(ownership, "linked_worktree_git_dir", lambda _path: linked_git_dir)
    monkeypatch.setattr(ownership, "_mirror_path_from_linked_git_dir", lambda _path: mirror_path)

    with pytest.raises(ValueError, match="expected parent"):
        ownership._validated_layout_mirror_for_worktree(worktree_path, "ws")  # noqa: SLF001


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_allows_verified_numeric_worktree_suffix(
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
    (linked_git_dir / "gitdir").write_text(
        f"{worktree_path / '.git'}\n",
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
    assert logger.exception_calls == []
    assert captured == [(mirror_root / "repo.git", worktree_path, linked_git_dir)]


@pytest.mark.unit
async def test_repair_agent_runtime_ownership_blocks_symlinked_git_backref(
    tmp_path: Path,
) -> None:
    workspace_id = "ws"
    worktrees_root = tmp_path / "workspace"
    mirror_root = worktrees_root.parent / "mirrors"
    worktree_path = worktrees_root / workspace_id
    other_worktree_path = worktrees_root / f"{workspace_id}-other"
    worktree_path.mkdir(parents=True)
    other_worktree_path.mkdir(parents=True)
    linked_git_dir = mirror_root / "repo.git" / "worktrees" / f"{workspace_id}1"
    linked_git_dir.mkdir(parents=True)
    other_git_file = other_worktree_path / ".git"
    other_git_file.write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )
    (worktree_path / ".git").symlink_to(other_git_file)
    (linked_git_dir / "gitdir").write_text(
        f"{other_git_file}\n",
        encoding="utf-8",
    )

    called = False

    def _repair_agent_writable_worktree(
        _layout_mirror: Path | None,
        _path: Path,
        linked_git_dir: Path | None = None,
    ) -> None:
        _ = linked_git_dir
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
async def test_repair_agent_runtime_ownership_blocks_numeric_worktree_suffix(
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

    called = False

    def _repair_agent_writable_worktree(
        _layout_mirror: Path | None,
        _path: Path,
        linked_git_dir: Path | None = None,
    ) -> None:
        _ = linked_git_dir
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
async def test_repair_agent_runtime_ownership_blocks_numeric_suffix_for_other_worktree(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_1"
    other_workspace_id = "ws_12"
    worktrees_root = tmp_path / "workspace"
    mirror_root = worktrees_root.parent / "mirrors"
    worktree_path = worktrees_root / workspace_id
    other_worktree_path = worktrees_root / other_workspace_id
    worktree_path.mkdir(parents=True)
    other_worktree_path.mkdir(parents=True)
    linked_git_dir = mirror_root / "repo.git" / "worktrees" / other_workspace_id
    linked_git_dir.mkdir(parents=True)
    (worktree_path / ".git").write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )
    (linked_git_dir / "gitdir").write_text(
        f"{other_worktree_path / '.git'}\n",
        encoding="utf-8",
    )

    called = False

    def _repair_agent_writable_worktree(
        _layout_mirror: Path | None,
        _path: Path,
        linked_git_dir: Path | None = None,
    ) -> None:
        _ = linked_git_dir
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
        _layout_mirror: Path | None,
        _path: Path,
        linked_git_dir: Path | None = None,
    ) -> None:
        _ = linked_git_dir
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
        _layout_mirror: Path | None,
        _path: Path,
        linked_git_dir: Path | None = None,
    ) -> None:
        _ = linked_git_dir
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
        _layout_mirror: Path | None,
        _path: Path,
        linked_git_dir: Path | None = None,
    ) -> None:
        _ = linked_git_dir
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
