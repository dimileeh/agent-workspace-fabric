"""GitManager tests against a real throwaway git repository.

We intentionally exercise the real ``git`` CLI rather than mock it — the whole
value of GitManager is correct handling of real worktree semantics, so mocking
would test nothing useful. Setup is fast (git init + two commits < 200 ms on
typical hardware) and each test gets an isolated ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from awf.node.git_manager import GitManager, GitOperationError


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
    """Create a minimal git repository with two commits on ``development``.

    Acts as the "origin" URL the GitManager will clone as a mirror.
    """
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


class TestEnsureMirror:
    @pytest.mark.unit
    async def test_clones_on_first_call(self, manager: GitManager, origin_repo: Path) -> None:
        mirror = await manager.ensure_mirror(str(origin_repo))
        assert mirror.exists()
        assert (mirror / "HEAD").exists()  # bare repo has HEAD at the top level

    @pytest.mark.unit
    async def test_updates_on_second_call(
        self, manager: GitManager, origin_repo: Path, tmp_path: Path
    ) -> None:
        mirror = await manager.ensure_mirror(str(origin_repo))
        first_call_path = mirror

        # Add a new commit on the origin.
        (origin_repo / "NEW.md").write_text("added later\n")
        _git(["add", "."], origin_repo)
        _git(["commit", "-q", "-m", "third"], origin_repo)

        mirror_again = await manager.ensure_mirror(str(origin_repo))
        assert mirror_again == first_call_path  # same path, no re-clone

        # The fetched commit is reachable from origin/development. Local
        # heads are deleted post-clone so they don't drift — base-branch
        # lookups go through the remote-tracking ref.
        rev_list = subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror_again),
                "rev-list",
                "--count",
                "origin/development",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert int(rev_list.stdout.strip()) == 3

    @pytest.mark.unit
    async def test_concurrent_first_calls_for_different_repos_do_not_collide(
        self, manager: GitManager, origin_repo: Path, tmp_path: Path
    ) -> None:
        other = tmp_path / "origin-2"
        other.mkdir()
        _git(["init", "-q", "-b", "main"], other)
        _git(["config", "user.name", "x"], other)
        _git(["config", "user.email", "x@x"], other)
        (other / "f").write_text("a\n")
        _git(["add", "."], other)
        _git(["commit", "-q", "-m", "a"], other)

        m1, m2 = await asyncio.gather(
            manager.ensure_mirror(str(origin_repo)),
            manager.ensure_mirror(str(other)),
        )
        assert m1 != m2
        assert m1.exists() and m2.exists()


class TestAddWorktree:
    @pytest.mark.unit
    async def test_creates_worktree_with_new_branch(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_abc123",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_abc123",
        )

        assert layout.worktree_path.exists()
        assert (layout.worktree_path / "README.md").read_text() == "second\n"

        branch = subprocess.run(
            ["git", "-C", str(layout.worktree_path), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert branch == "awf/ws_abc123"

    @pytest.mark.unit
    async def test_rejects_duplicate_worktree_path(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        await manager.add_worktree(
            workspace_id="ws_dup",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_dup",
        )
        with pytest.raises(GitOperationError) as exc:
            await manager.add_worktree(
                workspace_id="ws_dup",
                repo_url=str(origin_repo),
                base_branch="development",
                new_branch="awf/ws_dup-2",
            )
        assert exc.value.reason_code == "GIT_WORKTREE_ALREADY_EXISTS"

    @pytest.mark.unit
    async def test_rejects_missing_base_branch(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        with pytest.raises(GitOperationError) as exc:
            await manager.add_worktree(
                workspace_id="ws_missing",
                repo_url=str(origin_repo),
                base_branch="does-not-exist",
                new_branch="awf/ws_missing",
            )
        assert exc.value.reason_code == "GIT_BASE_BRANCH_MISSING"

    @pytest.mark.unit
    async def test_prepares_linked_worktree_git_paths_for_agent_user(
        self, origin_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chowned: list[tuple[Path, int, int]] = []
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(
            os,
            "chown",
            lambda path, uid, gid: chowned.append((Path(path), uid, gid)),
        )
        manager = GitManager(
            tmp_path / "awf-work",
            worktree_owner_uid=1000,
            worktree_owner_gid=1000,
        )

        layout = await manager.add_worktree(
            workspace_id="ws_agent_owner",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_agent_owner",
        )

        assert (layout.worktree_path, 1000, 1000) in chowned
        assert (layout.mirror_path, 1000, 1000) in chowned
        assert (layout.mirror_path / "objects", 1000, 1000) in chowned
        assert (layout.mirror_path / "refs", 1000, 1000) in chowned
        assert (layout.mirror_path / "worktrees", 1000, 1000) in chowned
        assert (
            layout.mirror_path / "worktrees" / "ws_agent_owner",
            1000,
            1000,
        ) in chowned
        assert (layout.worktree_path / "README.md", 1000, 1000) in chowned
        object_files = [
            path
            for path in (layout.mirror_path / "objects").glob("*/*")
            if path.is_file()
        ]
        assert object_files
        assert all((path, 1000, 1000) not in chowned for path in object_files)

    @pytest.mark.unit
    async def test_agent_owner_repair_skips_unwritable_loose_object_files(
        self, origin_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager_without_owner = GitManager(tmp_path / "awf-work")
        mirror = await manager_without_owner.ensure_mirror(str(origin_repo))
        protected_object = next(
            path for path in (mirror / "objects").glob("*/*") if path.is_file()
        )

        chowned: list[Path] = []

        def fake_chown(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], uid: int, gid: int) -> None:
            target = Path(path)
            if target == protected_object:
                raise PermissionError(target)
            chowned.append(target)

        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(os, "chown", fake_chown)
        manager = GitManager(
            tmp_path / "awf-work",
            worktree_owner_uid=1000,
            worktree_owner_gid=1000,
        )

        layout = await manager.add_worktree(
            workspace_id="ws_object_skip",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_object_skip",
        )

        assert layout.worktree_path in chowned
        assert mirror / "objects" in chowned
        assert protected_object not in chowned


class TestRemoveWorktree:
    @pytest.mark.unit
    async def test_removes_existing_worktree(self, manager: GitManager, origin_repo: Path) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_rm",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_rm",
        )
        assert layout.worktree_path.exists()

        await manager.remove_worktree(workspace_id="ws_rm", repo_url=str(origin_repo))
        assert not layout.worktree_path.exists()

    @pytest.mark.unit
    async def test_missing_worktree_is_noop(self, manager: GitManager, origin_repo: Path) -> None:
        # Ensure mirror exists so only worktree is "missing."
        await manager.ensure_mirror(str(origin_repo))
        # Should not raise.
        await manager.remove_worktree(workspace_id="ws_never_created", repo_url=str(origin_repo))


class TestHeadSha:
    @pytest.mark.unit
    async def test_returns_current_head(self, manager: GitManager, origin_repo: Path) -> None:
        layout = await manager.add_worktree(
            workspace_id="ws_head",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_head",
        )
        sha = await manager.head_sha(workspace_id="ws_head")
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

        expected = subprocess.run(
            ["git", "-C", str(layout.worktree_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert sha == expected


class TestGitEnvironment:
    @pytest.mark.unit
    async def test_run_uses_configured_environment(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        manager = GitManager(tmp_path / "work", env={"HOME": str(home), "AWF_TEST_ENV": "ok"})

        result = await manager._run(  # noqa: SLF001 - narrow regression for subprocess env.
            ["sh", "-c", "printf '%s:%s' \"$HOME\" \"$AWF_TEST_ENV\""],
            operation="env",
        )

        assert result.stdout == f"{home}:ok"
