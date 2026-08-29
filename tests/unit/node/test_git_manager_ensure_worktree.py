"""GitManager ensure_worktree restore used by hosted monitor recovery."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from awf.node.git_manager import GitManager


def _git(args: list[str], cwd: Path) -> None:
    """Run a synchronous git command for fixture setup; fail loudly on error."""
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository with two commits on ``development``."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "AWF Test"], repo)
    _git(["config", "user.email", "awf@test.local"], repo)

    (repo / "README.md").write_text("first\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)

    (repo / "README.md").write_text("second\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "update"], repo)
    return repo


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "awf-work"
    d.mkdir()
    return d


@pytest.fixture
def manager(work_dir: Path) -> GitManager:
    return GitManager(work_dir)


class TestEnsureWorktree:
    """Idempotent worktree restore used by hosted monitor restart recovery."""

    @pytest.mark.unit
    async def test_ensure_worktree_no_op_when_checkout_valid(
        self, manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_ensure_valid",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_valid",
        )
        add_calls: list[str] = []

        async def _add(**kwargs: object) -> object:
            add_calls.append("add")
            raise AssertionError("add_worktree must not run")

        monkeypatch.setattr(manager, "add_worktree", _add)
        again = await manager.ensure_worktree(
            workspace_id="ws_ensure_valid",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_valid",
        )
        assert again.worktree_path == layout.worktree_path
        assert add_calls == []

    @pytest.mark.unit
    async def test_ensure_worktree_recreates_when_branch_mismatches(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        """Usable checkout on the wrong branch must not take the no-op path."""
        workspace_id = "ws_ensure_branch_mismatch"
        layout = await manager.add_worktree(
            workspace_id=workspace_id,
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )
        subprocess.run(
            ["git", "-C", str(layout.worktree_path), "checkout", "-B", "wrong-branch"],
            check=True,
            capture_output=True,
        )
        restored = await manager.ensure_worktree(
            workspace_id=workspace_id,
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )
        assert restored.worktree_path == layout.worktree_path
        assert restored.branch_name == f"awf/{workspace_id}"
        head = subprocess.run(
            ["git", "-C", str(restored.worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert head.stdout.strip() == f"awf/{workspace_id}"

    @pytest.mark.unit
    async def test_ensure_worktree_recreates_when_mirror_mismatches(
        self, manager: GitManager, origin_repo: Path, tmp_path: Path
    ) -> None:
        """Usable checkout from another mirror must not be accepted as this repo."""
        other = tmp_path / "origin_other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "development"], cwd=other, check=True)
        subprocess.run(
            ["git", "config", "user.name", "AWF Test"], cwd=other, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "awf@test.local"],
            cwd=other,
            check=True,
            capture_output=True,
        )
        (other / "README.md").write_text("other\n")
        subprocess.run(["git", "add", "."], cwd=other, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"], cwd=other, check=True, capture_output=True
        )

        workspace_id = "ws_ensure_mirror_mismatch"
        await manager.add_worktree(
            workspace_id=workspace_id,
            repo_url=str(other),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )
        restored = await manager.ensure_worktree(
            workspace_id=workspace_id,
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch=f"awf/{workspace_id}",
        )
        assert restored.mirror_path == manager._mirror_path(str(origin_repo))
        assert restored.branch_name == f"awf/{workspace_id}"
        assert (restored.worktree_path / "README.md").read_text() == "second\n"

    @pytest.mark.unit
    async def test_ensure_worktree_recreates_missing_checkout(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_ensure_missing",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_missing",
        )
        import shutil

        shutil.rmtree(layout.worktree_path)
        assert not layout.worktree_path.exists()

        restored = await manager.ensure_worktree(
            workspace_id="ws_ensure_missing",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_missing",
        )
        assert restored.worktree_path.is_dir()
        assert (restored.worktree_path / ".git").exists()
        sha = await manager.head_sha(workspace_id="ws_ensure_missing")
        assert len(sha) == 40

    @pytest.mark.unit
    async def test_ensure_worktree_prunes_stale_metadata_then_recreates(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_ensure_stale",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_stale",
        )
        import shutil

        # Delete the directory but leave mirror worktree admin metadata.
        shutil.rmtree(layout.worktree_path)
        # The mirror-side admin dir may already be pruned by git; recreate still works.

        restored = await manager.ensure_worktree(
            workspace_id="ws_ensure_stale",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_stale",
        )
        assert restored.worktree_path.is_dir()
        assert (restored.worktree_path / ".git").exists()

    @pytest.mark.unit
    async def test_ensure_worktree_fences_concurrent_recreate(
        self, manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stale + takeover restore must not interleave remove/delete/add phases."""
        layout = await manager.add_worktree(
            workspace_id="ws_ensure_fence",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_ensure_fence",
        )
        import shutil

        shutil.rmtree(layout.worktree_path)

        original_delete = manager._delete_local_branch_best_effort
        entered_delete = asyncio.Event()
        release_delete = asyncio.Event()
        delete_calls = 0

        async def _gated_delete(**kwargs: object) -> None:
            nonlocal delete_calls
            delete_calls += 1
            if delete_calls == 1:
                entered_delete.set()
                await release_delete.wait()
            await original_delete(**kwargs)

        monkeypatch.setattr(manager, "_delete_local_branch_best_effort", _gated_delete)

        task_a = asyncio.create_task(
            manager.ensure_worktree(
                workspace_id="ws_ensure_fence",
                repo_url=str(origin_repo),
                base_branch="development",
                new_branch="awf/ws_ensure_fence",
            )
        )
        await asyncio.wait_for(entered_delete.wait(), timeout=5)

        task_b = asyncio.create_task(
            manager.ensure_worktree(
                workspace_id="ws_ensure_fence",
                repo_url=str(origin_repo),
                base_branch="development",
                new_branch="awf/ws_ensure_fence",
            )
        )
        # Without a continuous fence, B can recreate while A is paused between
        # remove and add; A then deletes B's branch/checkout on resume.
        await asyncio.sleep(0.1)
        assert not task_b.done(), "takeover restore raced ahead of fenced recreate"

        release_delete.set()
        layouts = await asyncio.gather(task_a, task_b)
        assert layouts[0].worktree_path == layout.worktree_path
        assert layouts[1].worktree_path == layout.worktree_path
        assert layout.worktree_path.is_dir()
        assert (layout.worktree_path / ".git").exists()
        sha = await manager.head_sha(workspace_id="ws_ensure_fence")
        assert len(sha) == 40
