"""Pre-push validation fix-pass re-parent git environment tests."""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import AsyncioSubprocessRunner, FakeCommandRunner
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)
from tests.unit.runtime.test_pr_monitor_pre_push_validation import _mark_git_worktree


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


def _init_repo_with_lateral_tip(tmp_path: Path) -> tuple[Path, str, str]:
    """Return ``(repo, ancestor_sha, lateral_sha)`` where lateral is not a descendant."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    _git(repo, "config", "advice.graftFileDeprecated", "false")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "ancestor")
    ancestor = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "--orphan", "lateral", "-q")
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "lateral tip")
    lateral = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, ancestor, lateral


@pytest.mark.unit
async def test_reparent_fix_pass_commit_strips_git_object_lookup_env(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-parenting must not write replacement commits into private git object dirs."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    fix_start_head = "1" * 40
    current_head = "2" * 40
    current_tree = "a" * 40
    start_tree = "b" * 40
    new_sha = "9" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{current_tree}\n")
    cmd.queue_result(returncode=0, stdout=f"{start_tree}\n")
    cmd.queue_result(returncode=0, stdout="agent body line\n")
    cmd.queue_result(returncode=0, stdout=f"{new_sha}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {new_sha[:8]}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    head, no_net_change, failure_reason = await pre_push_validation._reparent_fix_pass_commit(
        runner,
        workspace_id="workspace",
        worktree_path=worktree,
        fix_start_head=fix_start_head,
        current_head=current_head,
        pass_number=1,
        task_tag=None,
    )

    assert head == new_sha
    assert no_net_change is False
    assert failure_reason is None
    for call in cmd.calls:
        assert call.env is not None
        assert "GIT_OBJECT_DIRECTORY" not in call.env
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in call.env


@pytest.mark.unit
async def test_head_descends_from_disables_replace_and_graft_overrides(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ancestry proof must ignore refs/replace and other mutable parent overrides.

    Regression for PRRT_kwDOSJAM6s6ZlE3n / Bugbot graft fallback: merely popping
    ``GIT_GRAFT_FILE`` falls back to ``$GIT_DIR/info/grafts``. Force-disable the
    default graft source via the OS null device, keep replace objects off, and
    strip replace-base / object-lookup overrides.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    monkeypatch.setenv("GIT_GRAFT_FILE", "/tmp/agent-grafts")
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/replace")
    # A poisoned host env must not leave replace objects enabled.
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    ancestor = "1" * 40
    descendant = "2" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._head_descends_from(
        runner,
        worktree_path=worktree,
        ancestor=ancestor,
        descendant=descendant,
    )

    assert len(cmd.calls) == 1
    call = cmd.calls[0]
    assert "merge-base" in call.args
    assert "--is-ancestor" in call.args
    assert call.env is not None
    assert call.env.get("GIT_NO_REPLACE_OBJECTS") == "1"
    assert call.env.get("GIT_GRAFT_FILE") == os.devnull
    assert "GIT_REPLACE_REF_BASE" not in call.env
    assert "GIT_OBJECT_DIRECTORY" not in call.env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in call.env


@pytest.mark.unit
async def test_head_descends_from_rejects_replace_and_default_info_graft_forgery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-repo proof: refs/replace and default info/grafts cannot fake ancestry."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    monkeypatch.delenv("GIT_GRAFT_FILE", raising=False)
    monkeypatch.delenv("GIT_REPLACE_REF_BASE", raising=False)

    repo, ancestor, lateral = _init_repo_with_lateral_tip(tmp_path)
    # Plant default info/grafts that would make lateral appear descended from ancestor.
    info_dir = repo / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "grafts").write_text(f"{lateral} {ancestor}\n", encoding="utf-8")
    # Also plant a replace ref with the same forged parentage.
    tree = _git(repo, "rev-parse", f"{lateral}^{{tree}}").stdout.strip()
    forged = _git(repo, "commit-tree", tree, "-p", ancestor, "-m", "forged").stdout.strip()
    _git(repo, "update-ref", f"refs/replace/{lateral}", forged)

    # Unhardened git would accept the forged parentage.
    forged_check = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, lateral],
        check=False,
        capture_output=True,
        text=True,
    )
    assert forged_check.returncode == 0

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._head_descends_from(
        runner,
        worktree_path=repo,
        ancestor=ancestor,
        descendant=lateral,
    )
