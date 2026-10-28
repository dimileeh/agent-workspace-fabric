"""Local PR-monitor resume locks the kept checkout's worktree registration (#1133).

Workspaces registered before AWF locked its linked worktrees stay exposed to an
in-container ``git worktree prune`` until a reuse locks them. Uses real ``git``
for the mirror and checkout at the paths the GitManager-bound resolver derives.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.node import git_manager_registration as registration
from awf.node.git_manager import GitManager
from tests.unit.control.test_executor_monitor_recovery_parts.test_monitor_resume_restart_escalation import (  # noqa: E501
    _executor,
    _runtime_unusable,
    _ScriptedCompose,
    _seed,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (_runtime_unusable, factory, fake)


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def _legacy_unlocked_checkout(tmp_path: Path, git: GitManager, ws_id: str) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    _git("init", "-q", "-b", "development", str(origin))
    _git(
        "-C",
        str(origin),
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    admin, mirror = git.worktree_activity_git_roots(
        workspace_id=ws_id, repo_url="git@github.com:x/y.git"
    )
    _git("clone", "-q", "--bare", str(origin), str(mirror))
    checkout = git.get_worktree_path(ws_id)
    _git(
        "--git-dir",
        str(mirror),
        "worktree",
        "add",
        "-q",
        "-b",
        "awf/x",
        str(checkout),
        "development",
    )
    assert admin.is_dir()
    assert not (admin / "locked").exists()
    return admin, checkout


@pytest.mark.unit
async def test_local_resume_locks_a_legacy_unlocked_registration(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    compose.fail = False
    executor = _executor(fake, factory, tmp_path, compose)
    git = GitManager(executor._config.worktrees_root.parent)
    executor._worktree_activity_git_roots = git.worktree_activity_git_roots
    admin, checkout = _legacy_unlocked_checkout(tmp_path, git, ws_id)
    assert checkout == executor._config.worktrees_root / ws_id

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    assert (admin / "locked").read_text(encoding="utf-8").strip() == (
        registration.awf_worktree_lock_reason(ws_id)
    )
