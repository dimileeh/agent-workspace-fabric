"""GitManager HEAD-object verification tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.node import git_manager as git_module
from awf.node.git_manager import GitManager


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
def work_dir(tmp_path: Path) -> Path:
    path = tmp_path / "awf-work"
    path.mkdir()
    return path


class TestVerifyHeadObjectExists:
    @pytest.mark.unit
    async def test_succeeds_for_valid_head(self, origin_repo: Path, work_dir: Path) -> None:
        manager = GitManager(work_dir)
        layout = await manager.add_worktree(
            workspace_id="ws_verify_head",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_verify_head",
        )

        result = await git_module.verify_head_object_exists(layout.worktree_path)

        assert result is True

    @pytest.mark.unit
    async def test_fails_for_missing_object(self, origin_repo: Path, work_dir: Path) -> None:
        manager = GitManager(work_dir)
        layout = await manager.add_worktree(
            workspace_id="ws_missing_obj",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_missing_obj",
        )

        fake_sha = "deadbeef" * 5
        ref_path = layout.mirror_path / "refs" / "heads" / layout.branch_name
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        ref_path.write_text(fake_sha + "\n")

        result = await git_module.verify_head_object_exists(layout.worktree_path)

        assert result is False

    @pytest.mark.unit
    async def test_ignores_inherited_object_lookup_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        origin_repo: Path,
        work_dir: Path,
        tmp_path: Path,
    ) -> None:
        manager = GitManager(work_dir)
        layout = await manager.add_worktree(
            workspace_id="ws_object_env",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_object_env",
        )

        alternate_repo = tmp_path / "alternate"
        alternate_repo.mkdir()
        _git(["init", "-q", "-b", "development"], alternate_repo)
        _git(["config", "user.name", "AWF Test"], alternate_repo)
        _git(["config", "user.email", "awf@test.local"], alternate_repo)
        (alternate_repo / "README.md").write_text("alternate\n")
        _git(["add", "."], alternate_repo)
        _git(["commit", "-q", "-m", "alternate"], alternate_repo)
        alternate_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=alternate_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        ref_path = layout.mirror_path / "refs" / "heads" / layout.branch_name
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        ref_path.write_text(alternate_sha + "\n")
        alternate_objects = str(alternate_repo / ".git" / "objects")
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", alternate_objects)
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", alternate_objects)

        result = await git_module.verify_head_object_exists(layout.worktree_path)

        assert result is False
