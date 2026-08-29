"""GitManager ensure_worktree linked-checkout usability regressions."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import threading
from pathlib import Path

import pytest

from awf.node.git_manager import GitManager, GitOperationError, _worktree_checkout_is_usable


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "AWF Test"], repo)
    _git(["config", "user.email", "awf@test.local"], repo)
    (repo / "README.md").write_text("first\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture
def manager(tmp_path: Path) -> GitManager:
    work_dir = tmp_path / "awf-work"
    work_dir.mkdir()
    return GitManager(work_dir)


@pytest.mark.unit
async def test_ensure_worktree_usability_probe_does_not_block_event_loop(
    manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkout identity probes must run off the asyncio event loop.

    A hung HEAD/branch probe (subprocess timeout up to 5s) must not stall
    unrelated async work on the same loop during hosted monitor recovery.
    """
    import awf.node.git_manager as git_manager_mod

    workspace_id = "ws_ensure_probe_offloop"
    layout = await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    assert _worktree_checkout_is_usable(layout.worktree_path)

    probe_started = threading.Event()
    release_probe = threading.Event()
    observed_event_loop_progress = False
    real_match = git_manager_mod._worktree_checkout_matches_ensure_request

    def _blocking_match(path: Path, *, expected_mirror: Path, expected_branch: str) -> bool:
        probe_started.set()
        if not release_probe.wait(timeout=0.5):
            raise AssertionError("event loop stalled while identity probe was blocked")
        return real_match(path, expected_mirror=expected_mirror, expected_branch=expected_branch)

    monkeypatch.setattr(
        git_manager_mod, "_worktree_checkout_matches_ensure_request", _blocking_match
    )

    async def _release_probe_after_loop_progress() -> None:
        nonlocal observed_event_loop_progress
        while not probe_started.is_set():
            await asyncio.sleep(0)
        observed_event_loop_progress = True
        release_probe.set()

    releaser = asyncio.create_task(_release_probe_after_loop_progress())
    try:
        restored = await manager.ensure_worktree(
            workspace_id=workspace_id,
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )
    finally:
        if not releaser.done():
            releaser.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await releaser

    assert observed_event_loop_progress is True
    assert restored.worktree_path == layout.worktree_path
    assert _worktree_checkout_is_usable(restored.worktree_path)


@pytest.mark.unit
async def test_ensure_worktree_recreates_when_linked_git_dir_missing(
    manager: GitManager, origin_repo: Path
) -> None:
    """Checkout dir survives but mirror-side admin dir is gone → reconstruct."""
    import shutil

    layout = await manager.add_worktree(
        workspace_id="ws_ensure_dangling",
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch="awf/ws_ensure_dangling",
    )
    linked_git_dir = layout.mirror_path / "worktrees" / "ws_ensure_dangling"
    assert linked_git_dir.is_dir()
    assert _worktree_checkout_is_usable(layout.worktree_path)

    shutil.rmtree(linked_git_dir)
    assert layout.worktree_path.is_dir()
    assert (layout.worktree_path / ".git").is_file()
    assert not _worktree_checkout_is_usable(layout.worktree_path)

    restored = await manager.ensure_worktree(
        workspace_id="ws_ensure_dangling",
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch="awf/ws_ensure_dangling",
    )
    assert restored.worktree_path.is_dir()
    assert _worktree_checkout_is_usable(restored.worktree_path)
    sha = await manager.head_sha(workspace_id="ws_ensure_dangling")
    assert len(sha) == 40


@pytest.mark.unit
def test_worktree_checkout_usable_requires_reciprocal_registration(tmp_path: Path) -> None:
    worktree = tmp_path / "worktrees" / "ws_reciprocal"
    linked = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws_reciprocal"
    worktree.mkdir(parents=True)
    linked.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {linked}\n", encoding="utf-8")
    # Back-ref points at a different checkout → not usable.
    other = tmp_path / "worktrees" / "other" / ".git"
    other.parent.mkdir(parents=True)
    (linked / "gitdir").write_text(f"{other}\n", encoding="utf-8")
    assert not _worktree_checkout_is_usable(worktree)

    # Reciprocal registration alone is insufficient: without resolvable Git
    # metadata (HEAD), the checkout must not take the ensure_worktree no-op.
    (linked / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    assert not _worktree_checkout_is_usable(worktree)

    (linked / "gitdir").unlink()
    assert not _worktree_checkout_is_usable(worktree)


@pytest.mark.unit
async def test_ensure_worktree_recreates_when_linked_head_missing(
    manager: GitManager, origin_repo: Path
) -> None:
    """Admin dir + reciprocal gitdir survive but HEAD is gone → reconstruct."""
    workspace_id = "ws_ensure_missing_head"
    layout = await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    linked_git_dir = layout.mirror_path / "worktrees" / workspace_id
    head_path = linked_git_dir / "HEAD"
    assert head_path.is_file()
    assert _worktree_checkout_is_usable(layout.worktree_path)

    head_path.unlink()
    assert (layout.worktree_path / ".git").is_file()
    assert linked_git_dir.is_dir()
    assert (linked_git_dir / "gitdir").is_file()
    assert not _worktree_checkout_is_usable(layout.worktree_path)

    restored = await manager.ensure_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    assert restored.worktree_path.is_dir()
    assert _worktree_checkout_is_usable(restored.worktree_path)
    sha = await manager.head_sha(workspace_id=workspace_id)
    assert len(sha) == 40


@pytest.mark.unit
async def test_corrupt_gitfile_worktree_validation_failure_is_reclaimed(
    manager: GitManager, origin_repo: Path
) -> None:
    """Corrupt gitfile (missing linked HEAD) validation must be reclaimed.

    When the checkout ``.git`` gitfile exists but linked admin metadata is
    unusable, ``git worktree remove`` reports ``.git`` is not a .git file
    (error code 7). Ensure reclaim + prune still succeed so
    ``ensure_worktree`` can recreate.
    """
    await manager.ensure_mirror(str(origin_repo))
    worktree_path = manager._worktrees_dir / "ws_corrupt_gitfile"
    worktree_path.mkdir(parents=True)
    (worktree_path / ".git").write_text(
        "gitdir: /nonexistent/mirror/worktrees/ws_corrupt_gitfile\n",
        encoding="utf-8",
    )
    (worktree_path / "leftover.txt").write_text("stale\n")

    pruned: list[str] = []

    async def _stale_run(args: list[str], *, operation: str):  # type: ignore[no-untyped-def]
        """Test helper for corrupt-gitfile remove."""
        if operation == "worktree.remove":
            raise GitOperationError(
                operation=operation,
                returncode=128,
                stdout="",
                stderr=(
                    "fatal: validation failed, cannot remove working tree: "
                    f"'{worktree_path}/.git' is not a .git file, error code 7"
                ),
            )
        if operation == "worktree.prune":
            pruned.append(operation)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected operation {operation}")

    manager._run = _stale_run  # type: ignore[method-assign]

    await manager.remove_worktree(
        workspace_id="ws_corrupt_gitfile",
        repo_url=str(origin_repo),
    )

    assert pruned == ["worktree.prune"]
    assert not worktree_path.exists()


@pytest.mark.unit
async def test_unreadable_gitfile_validation_failure_is_not_reclaimed(
    manager: GitManager, origin_repo: Path
) -> None:
    """Unreadable ``.git`` (error code 3) must fail closed, not rmtree.

    Git reports the same ``is not a .git file`` phrase for temporary permission
    failures (error code 3) as for corrupt linked metadata (error code 7). Only
    the latter is a proven stale-metadata reclaim case; reclaiming on code 3
    would erase a live checkout and uncommitted repair.
    """
    await manager.ensure_mirror(str(origin_repo))
    worktree_path = manager._worktrees_dir / "ws_unreadable_gitfile"
    worktree_path.mkdir(parents=True)
    (worktree_path / ".git").write_text(
        "gitdir: /nonexistent/mirror/worktrees/ws_unreadable_gitfile\n",
        encoding="utf-8",
    )
    (worktree_path / "repair.txt").write_text("do-not-delete\n")

    async def _unreadable_run(args: list[str], *, operation: str):  # type: ignore[no-untyped-def]
        """Test helper for unreadable-gitfile remove."""
        if operation == "worktree.remove":
            raise GitOperationError(
                operation=operation,
                returncode=128,
                stdout="",
                stderr=(
                    "fatal: validation failed, cannot remove working tree: "
                    f"'{worktree_path}/.git' is not a .git file, error code 3"
                ),
            )
        raise AssertionError(f"unexpected operation {operation}")

    manager._run = _unreadable_run  # type: ignore[method-assign]

    with pytest.raises(GitOperationError) as excinfo:
        await manager.remove_worktree(
            workspace_id="ws_unreadable_gitfile",
            repo_url=str(origin_repo),
        )

    assert "error code 3" in excinfo.value.stderr
    assert worktree_path.exists()
    assert (worktree_path / "repair.txt").read_text() == "do-not-delete\n"
    assert (worktree_path / ".git").is_file()


@pytest.mark.unit
def test_worktree_checkout_probe_indeterminate_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timeout / OSError on the HEAD probe must fail closed, not pretend corrupt."""
    import awf.node.git_manager_linked as linked
    from awf.node.git_manager import GitOperationError

    worktree = tmp_path / "worktrees" / "ws_probe_fail"
    linked_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws_probe_fail"
    worktree.mkdir(parents=True)
    linked_dir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {linked_dir}\n", encoding="utf-8")
    (linked_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (linked_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    def _timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(linked.subprocess, "run", _timeout)
    with pytest.raises(GitOperationError) as timeout_info:
        _worktree_checkout_is_usable(worktree)
    assert timeout_info.value.operation == "worktree.checkout_probe"
    assert "timed out" in timeout_info.value.stderr

    def _os_error(*_args: object, **_kwargs: object) -> None:
        raise OSError("git missing")

    monkeypatch.setattr(linked.subprocess, "run", _os_error)
    with pytest.raises(GitOperationError) as os_info:
        _worktree_checkout_is_usable(worktree)
    assert os_info.value.operation == "worktree.checkout_probe"
    assert "could not probe" in os_info.value.stderr


@pytest.mark.unit
async def test_ensure_worktree_preserves_checkout_when_head_probe_times_out(
    manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Indeterminate HEAD probe must not force-remove a live linked checkout."""
    import awf.node.git_manager_linked as linked
    from awf.node.git_manager import GitOperationError

    workspace_id = "ws_ensure_probe_timeout"
    layout = await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    repair_marker = layout.worktree_path / "uncommitted_monitor_repair.txt"
    repair_marker.write_text("do not discard\n", encoding="utf-8")
    assert _worktree_checkout_is_usable(layout.worktree_path)

    def _timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(linked.subprocess, "run", _timeout)
    with pytest.raises(GitOperationError) as raised:
        await manager.ensure_worktree(
            workspace_id=workspace_id,
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )
    assert raised.value.operation == "worktree.checkout_probe"
    assert layout.worktree_path.is_dir()
    assert repair_marker.is_file()
    assert repair_marker.read_text(encoding="utf-8") == "do not discard\n"


@pytest.mark.unit
def test_worktree_checkout_unreadable_reciprocal_gitdir_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable reciprocal gitdir must raise, not look like confirmed-stale."""
    import awf.node.git_manager_linked as linked

    worktree = tmp_path / "worktrees" / "ws_unreadable_gitdir"
    linked_dir = tmp_path / "mirrors" / "repo.git" / "worktrees" / "ws_unreadable_gitdir"
    worktree.mkdir(parents=True)
    linked_dir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {linked_dir}\n", encoding="utf-8")
    gitdir_file = linked_dir / "gitdir"
    gitdir_file.write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (linked_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    real_read_text = Path.read_text

    def _read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == gitdir_file:
            raise PermissionError("Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    with pytest.raises(GitOperationError) as raised:
        _worktree_checkout_is_usable(worktree)
    assert raised.value.operation == "worktree.hooks_path_probe"
    assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
    assert "cannot access linked-worktree gitdir back-reference" in raised.value.stderr
    assert not linked._is_stale_linked_worktree_metadata_error(raised.value)


@pytest.mark.unit
async def test_ensure_worktree_preserves_checkout_when_reciprocal_gitdir_unreadable(
    manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable reciprocal gitdir must not force-remove a live linked checkout."""
    workspace_id = "ws_ensure_unreadable_gitdir"
    layout = await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    repair_marker = layout.worktree_path / "uncommitted_monitor_repair.txt"
    repair_marker.write_text("do not discard\n", encoding="utf-8")
    assert _worktree_checkout_is_usable(layout.worktree_path)

    linked_git_dir = (layout.worktree_path / ".git").read_text(encoding="utf-8").strip()
    assert linked_git_dir.startswith("gitdir: ")
    gitdir_file = Path(linked_git_dir.removeprefix("gitdir: ").strip()) / "gitdir"
    assert gitdir_file.is_file()

    real_read_text = Path.read_text

    def _read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == gitdir_file:
            raise PermissionError("Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    with pytest.raises(GitOperationError) as raised:
        await manager.ensure_worktree(
            workspace_id=workspace_id,
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )
    assert raised.value.operation == "worktree.hooks_path_probe"
    assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
    assert "cannot access linked-worktree gitdir back-reference" in raised.value.stderr
    assert layout.worktree_path.is_dir()
    assert repair_marker.is_file()
    assert repair_marker.read_text(encoding="utf-8") == "do not discard\n"


@pytest.mark.unit
def test_worktree_checkout_rejects_standalone_git_directory(tmp_path: Path) -> None:
    """A normal clone (.git dir) must not count as a usable managed worktree."""
    worktree = tmp_path / "worktrees" / "ws_standalone"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    assert not _worktree_checkout_is_usable(worktree)


@pytest.mark.unit
async def test_ensure_worktree_fails_closed_when_standalone_clone_occupies_path(
    manager: GitManager, origin_repo: Path
) -> None:
    """Standalone clone at managed path must not be erased during restore recreate."""
    import shutil

    from awf.node.git_manager import GitOperationError

    workspace_id = "ws_ensure_standalone"
    layout = await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    assert _worktree_checkout_is_usable(layout.worktree_path)

    # Simulate an incomplete external restore: wipe the linked checkout and leave
    # an unrelated standalone clone at the managed path.
    shutil.rmtree(layout.worktree_path)
    layout.worktree_path.mkdir(parents=True)
    _git(["init", "-q", "-b", "development"], layout.worktree_path)
    _git(["config", "user.name", "AWF Test"], layout.worktree_path)
    _git(["config", "user.email", "awf@test.local"], layout.worktree_path)
    (layout.worktree_path / "README.md").write_text("orphan clone\n")
    _git(["add", "."], layout.worktree_path)
    _git(["commit", "-q", "-m", "orphan"], layout.worktree_path)
    assert (layout.worktree_path / ".git").is_dir()
    assert not _worktree_checkout_is_usable(layout.worktree_path)

    with pytest.raises(GitOperationError) as excinfo:
        await manager.ensure_worktree(
            workspace_id=workspace_id,
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )

    assert excinfo.value.reason_code == "GIT_WORKTREE_STANDALONE_REPO"
    assert (layout.worktree_path / ".git").is_dir()
    assert (layout.worktree_path / "README.md").read_text() == "orphan clone\n"


@pytest.mark.unit
async def test_ensure_worktree_accepts_concurrent_create_when_checkout_usable(
    manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GIT_WORKTREE_ALREADY_EXISTS after a racing recreate is success when usable."""
    import shutil

    workspace_id = "ws_ensure_race_exists"
    layout = await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    shutil.rmtree(layout.worktree_path)
    original_add = manager.add_worktree

    async def _racing_add(**kwargs: object) -> object:
        # Simulate another restorer finishing between our remove and add.
        restored = await original_add(
            workspace_id=workspace_id,
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )
        assert _worktree_checkout_is_usable(restored.worktree_path)
        raise GitOperationError(
            operation="worktree.add",
            returncode=128,
            stdout="",
            stderr=f"'{layout.worktree_path}' already exists",
            reason_code="GIT_WORKTREE_ALREADY_EXISTS",
        )

    monkeypatch.setattr(manager, "add_worktree", _racing_add)
    result = await manager.ensure_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    assert result.worktree_path == layout.worktree_path
    assert _worktree_checkout_is_usable(result.worktree_path)


@pytest.mark.unit
async def test_ensure_worktree_retries_once_on_branch_name_collision(
    manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leftover branch collision on add deletes the branch and retries once."""
    import shutil

    from awf.node.git_manager import WorktreeLayout

    workspace_id = "ws_ensure_branch_collision"
    layout = await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    shutil.rmtree(layout.worktree_path)

    calls = {"add": 0}
    original_add = manager.add_worktree
    original_delete = manager._delete_local_branch_best_effort
    deleted: list[str] = []

    async def _colliding_add(**kwargs: object) -> WorktreeLayout:
        calls["add"] += 1
        if calls["add"] == 1:
            raise GitOperationError(
                operation="worktree.add",
                returncode=128,
                stdout="",
                stderr=f"fatal: a branch named 'awf/{workspace_id}' already exists",
                reason_code="GIT_WORKTREE_ADD_FAILED",
            )
        return await original_add(**kwargs)

    async def _record_delete(*, mirror_path: Path, branch_name: str) -> None:
        deleted.append(branch_name)
        await original_delete(mirror_path=mirror_path, branch_name=branch_name)

    monkeypatch.setattr(manager, "add_worktree", _colliding_add)
    monkeypatch.setattr(manager, "_delete_local_branch_best_effort", _record_delete)

    restored = await manager.ensure_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    assert calls["add"] == 2
    # Pre-add cleanup plus the collision-retry delete.
    assert deleted.count(f"awf/{workspace_id}") >= 2
    assert restored.worktree_path.is_dir()
    assert _worktree_checkout_is_usable(restored.worktree_path)


@pytest.mark.unit
async def test_ensure_worktree_reraises_already_exists_when_checkout_unusable(
    manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GIT_WORKTREE_ALREADY_EXISTS without a usable checkout must not be treated as success."""
    import shutil

    workspace_id = "ws_ensure_exists_unusable"
    layout = await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    shutil.rmtree(layout.worktree_path)

    async def _already_exists(**kwargs: object) -> object:
        layout.worktree_path.mkdir(parents=True)
        (layout.worktree_path / "stale.txt").write_text("not a worktree\n")
        raise GitOperationError(
            operation="worktree.add",
            returncode=128,
            stdout="",
            stderr=f"'{layout.worktree_path}' already exists",
            reason_code="GIT_WORKTREE_ALREADY_EXISTS",
        )

    monkeypatch.setattr(manager, "add_worktree", _already_exists)
    with pytest.raises(GitOperationError) as raised:
        await manager.ensure_worktree(
            workspace_id=workspace_id,
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )
    assert raised.value.reason_code == "GIT_WORKTREE_ALREADY_EXISTS"
    assert not _worktree_checkout_is_usable(layout.worktree_path)


@pytest.mark.unit
async def test_ensure_worktree_reraises_unrelated_add_failure(
    manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-race add failures must propagate unchanged from ensure_worktree."""
    import shutil

    workspace_id = "ws_ensure_add_fail"
    layout = await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )
    shutil.rmtree(layout.worktree_path)

    async def _boom(**kwargs: object) -> object:
        raise GitOperationError(
            operation="worktree.add",
            returncode=128,
            stdout="",
            stderr="fatal: remote tip vanished",
            reason_code="GIT_FETCH_BASE_FAILED",
        )

    monkeypatch.setattr(manager, "add_worktree", _boom)
    with pytest.raises(GitOperationError) as raised:
        await manager.ensure_worktree(
            workspace_id=workspace_id,
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )
    assert raised.value.reason_code == "GIT_FETCH_BASE_FAILED"


@pytest.mark.unit
async def test_delete_local_branch_best_effort_noop_when_mirror_missing(
    manager: GitManager, tmp_path: Path
) -> None:
    """Missing mirror directories are a no-op so recreate can ensure_mirror next."""
    missing = tmp_path / "no-such-mirror.git"
    await manager._delete_local_branch_best_effort(
        mirror_path=missing,
        branch_name="awf/ws_gone",
    )
    assert not missing.exists()


@pytest.mark.unit
async def test_delete_local_branch_best_effort_swallows_delete_errors(
    manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent/checked-out branch delete errors are swallowed for best-effort cleanup."""
    mirror = await manager.ensure_mirror(str(origin_repo))

    async def _failing_run(args: list[str], *, operation: str):  # type: ignore[no-untyped-def]
        if operation == "mirror.branch_delete":
            raise GitOperationError(
                operation=operation,
                returncode=1,
                stdout="",
                stderr="error: branch 'awf/ws_x' not found",
                reason_code="GIT_BRANCH_DELETE_FAILED",
            )
        raise AssertionError(f"unexpected operation {operation}")

    monkeypatch.setattr(manager, "_run", _failing_run)
    await manager._delete_local_branch_best_effort(
        mirror_path=mirror,
        branch_name="awf/ws_x",
    )
