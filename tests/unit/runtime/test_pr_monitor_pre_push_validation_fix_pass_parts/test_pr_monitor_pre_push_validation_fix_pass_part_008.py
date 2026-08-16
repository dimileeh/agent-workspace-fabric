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


@pytest.mark.unit
async def test_commit_trees_differ_compares_resolved_trees(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._commit_trees_differ(
        runner,
        worktree_path=worktree,
        left="1" * 40,
        right="2" * 40,
    )
    assert len(cmd.calls) == 2
    assert f"{'1' * 40}^{{tree}}" in cmd.calls[0].args
    assert f"{'2' * 40}^{{tree}}" in cmd.calls[1].args


@pytest.mark.unit
async def test_commit_trees_differ_disables_replace_and_graft_overrides(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tree proof must ignore refs/replace and grafts the same way ancestry does.

    Regression for empty-descendant + ``refs/replace/<empty>`` FIXED forgery:
    hardened ancestry alone is insufficient if ``rev-parse ^{tree}`` still
    honors replace objects.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    monkeypatch.setenv("GIT_GRAFT_FILE", "/tmp/agent-grafts")
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/replace")
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._commit_trees_differ(
        runner,
        worktree_path=worktree,
        left="1" * 40,
        right="2" * 40,
    )

    assert len(cmd.calls) == 2
    for call in cmd.calls:
        assert call.env is not None
        assert call.env.get("GIT_NO_REPLACE_OBJECTS") == "1"
        assert call.env.get("GIT_GRAFT_FILE") == os.devnull
        assert "GIT_REPLACE_REF_BASE" not in call.env
        assert "GIT_OBJECT_DIRECTORY" not in call.env
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in call.env


@pytest.mark.unit
async def test_commit_trees_differ_false_for_identical_or_unresolved_trees(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    same_tree = "c" * 40

    identical = FakeCommandRunner()
    identical.queue_result(returncode=0, stdout=f"{same_tree}\n")
    identical.queue_result(returncode=0, stdout=f"{same_tree}\n")
    identical_runner = make_runner(
        factory=factory,
        cmd=identical,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_trees_differ(
        identical_runner,
        worktree_path=worktree,
        left="1" * 40,
        right="2" * 40,
    )

    missing_left = FakeCommandRunner()
    missing_left.queue_result(returncode=128, stdout="", stderr="missing")
    missing_left_runner = make_runner(
        factory=factory,
        cmd=missing_left,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_trees_differ(
        missing_left_runner,
        worktree_path=worktree,
        left="1" * 40,
        right="2" * 40,
    )

    missing_right = FakeCommandRunner()
    missing_right.queue_result(returncode=0, stdout=f"{same_tree}\n")
    missing_right.queue_result(returncode=128, stdout="", stderr="missing")
    missing_right_runner = make_runner(
        factory=factory,
        cmd=missing_right,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_trees_differ(
        missing_right_runner,
        worktree_path=worktree,
        left="1" * 40,
        right="2" * 40,
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


@pytest.mark.unit
async def test_commit_changes_present_in_head_accepts_preserved_and_rejects_revert(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Salvage content must remain detectable after later tips; reverts fail."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("salvaged\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "salvage")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "commit", "--allow-empty", "-qm", "empty on salvage")
    empty_on_salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "b.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "later preserving salvage")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "revert salvage content")
    reverted = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "c.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "unrelated after full revert")
    reverted_then_unrelated = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Pure revert tip: same tree as pre-salvage parent.
    _git(repo, "checkout", "-q", "-B", "pure-revert", salvage)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "pure revert to parent tree")
    pure_reverted = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=empty_on_salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=reverted,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=reverted_then_unrelated,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=pure_reverted,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=base,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_fail_closed_on_unresolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stdout="", stderr="missing")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit="1" * 40,
        head="2" * 40,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit="",
        head="2" * 40,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_fail_closed_on_empty_diff_or_missing_parent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    commit = "1" * 40
    head = "2" * 40
    commit_tree = "a" * 40
    head_tree = "b" * 40

    # Distinct trees, but first-parent resolution fails → fail closed.
    missing_parent = FakeCommandRunner()
    missing_parent.queue_result(returncode=0, stdout=f"{commit_tree}\n")
    missing_parent.queue_result(returncode=0, stdout=f"{head_tree}\n")
    missing_parent.queue_result(returncode=1, stdout="", stderr="root")
    runner = make_runner(
        factory=factory,
        cmd=missing_parent,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit=commit,
        head=head,
    )

    # Parent resolves, trees differ, but diff-tree returns no paths → fail closed.
    empty_paths = FakeCommandRunner()
    empty_paths.queue_result(returncode=0, stdout=f"{commit_tree}\n")
    empty_paths.queue_result(returncode=0, stdout=f"{head_tree}\n")
    empty_paths.queue_result(returncode=0, stdout=f"{'3' * 40}\n")
    empty_paths.queue_result(returncode=0, stdout=f"{'c' * 40}\n")
    empty_paths.queue_result(returncode=0, stdout="\n")
    runner = make_runner(
        factory=factory,
        cmd=empty_paths,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit=commit,
        head=head,
    )

    # diff-tree itself fails → fail closed.
    diff_tree_fail = FakeCommandRunner()
    diff_tree_fail.queue_result(returncode=0, stdout=f"{commit_tree}\n")
    diff_tree_fail.queue_result(returncode=0, stdout=f"{head_tree}\n")
    diff_tree_fail.queue_result(returncode=0, stdout=f"{'3' * 40}\n")
    diff_tree_fail.queue_result(returncode=0, stdout=f"{'c' * 40}\n")
    diff_tree_fail.queue_result(returncode=1, stdout="", stderr="boom")
    runner = make_runner(
        factory=factory,
        cmd=diff_tree_fail,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit=commit,
        head=head,
    )
