"""Regression: mirror clones must allow worktree pushes.

``git clone --mirror`` sets ``remote.origin.mirror=true`` which otherwise makes
refspec pushes fail with ``fatal: --mirror can't be combined with refspecs``.
GitManager.ensure_mirror must strip this flag so downstream worktrees can push.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.node.git_manager import GitManager


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.mark.unit
async def test_mirror_flag_is_stripped_after_clone(tmp_path: Path, origin_repo: Path) -> None:
    manager = GitManager(tmp_path / "work")
    mirror = await manager.ensure_mirror(str(origin_repo))

    result = subprocess.run(
        ["git", "--git-dir", str(mirror), "config", "--bool", "remote.origin.mirror"],
        capture_output=True,
        text=True,
    )
    # Expected: ``git config --unset`` removed the key entirely, so ``git config
    # --bool`` exits 1 with no stdout. Non-empty stdout (e.g. "true") would
    # reproduce the push failure.
    assert result.stdout.strip() == ""
    assert result.returncode == 1


@pytest.mark.unit
async def test_worktree_can_perform_refspec_operation_on_mirror(
    tmp_path: Path, origin_repo: Path
) -> None:
    """A smoke check: ``push --dry-run`` from a worktree must not trip on the mirror flag.

    We can't actually push in a unit test (would hit a remote), so we verify
    ``git push --dry-run origin HEAD:refs/heads/awf/test`` doesn't return the
    mirror-conflict error. It's fine if the command fails for other reasons
    (no remote reachable); the specific error we're guarding against is gone.
    """
    manager = GitManager(tmp_path / "work")
    layout = await manager.add_worktree(
        workspace_id="ws_mirror_check",
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch="awf/ws_mirror_check",
    )

    # Dry-run push — we don't actually have a real remote, but the failure
    # mode we care about happens before network I/O.
    result = subprocess.run(
        ["git", "-C", str(layout.worktree_path), "push", "--dry-run", "origin", "HEAD"],
        capture_output=True,
        text=True,
    )
    combined = result.stderr + result.stdout
    assert "--mirror can't be combined with refspecs" not in combined
