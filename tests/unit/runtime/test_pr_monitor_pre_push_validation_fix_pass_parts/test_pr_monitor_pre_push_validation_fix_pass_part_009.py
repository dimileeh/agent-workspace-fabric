"""Pre-push validation fix-pass tree comparison edge cases (part 9)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import AsyncioSubprocessRunner
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
async def test_commit_trees_differ_rejects_real_empty_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Real-repo proof: allow-empty advances HEAD with an unchanged tree."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "commit", "--allow-empty", "-qm", "empty")
    empty_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "content")
    content_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_trees_differ(
        runner,
        worktree_path=repo,
        left=start,
        right=empty_tip,
    )
    assert await pre_push_validation._commit_trees_differ(
        runner,
        worktree_path=repo,
        left=start,
        right=content_tip,
    )


@pytest.mark.unit
async def test_commit_trees_differ_rejects_empty_commit_with_replace_forgery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real empty tip + refs/replace to a contentful commit must stay rejected.

    Hardened ancestry still accepts the empty descendant; unhardened
    ``rev-parse ^{tree}`` would see the replacement tree and falsely differ.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    monkeypatch.delenv("GIT_GRAFT_FILE", raising=False)
    monkeypatch.delenv("GIT_REPLACE_REF_BASE", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    _git(repo, "config", "advice.graftFileDeprecated", "false")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "commit", "--allow-empty", "-qm", "empty")
    empty_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.txt").write_text("forged-content\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    content_tree = _git(repo, "write-tree").stdout.strip()
    forged = _git(
        repo,
        "commit-tree",
        content_tree,
        "-p",
        start,
        "-m",
        "forged-contentful",
    ).stdout.strip()
    _git(repo, "update-ref", f"refs/replace/{empty_tip}", forged)
    # Restore a clean worktree at the real empty tip without following replace.
    subprocess.run(
        ["git", "-C", str(repo), "reset", "--hard", empty_tip],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull},
    )

    # Unhardened git reports differing trees via the replace ref.
    unhardened = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            f"{start}^{{tree}}",
            f"{empty_tip}^{{tree}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unhardened.returncode == 0
    start_tree, empty_via_replace = unhardened.stdout.strip().splitlines()
    assert start_tree != empty_via_replace

    # Ancestry of the real empty tip still holds under the hardened check.
    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._head_descends_from(
        runner,
        worktree_path=repo,
        ancestor=start,
        descendant=empty_tip,
    )
    assert not await pre_push_validation._commit_trees_differ(
        runner,
        worktree_path=repo,
        left=start,
        right=empty_tip,
    )
