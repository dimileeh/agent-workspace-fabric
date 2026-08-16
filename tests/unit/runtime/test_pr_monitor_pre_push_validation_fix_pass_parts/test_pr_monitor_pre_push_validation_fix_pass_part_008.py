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
def test_parse_ls_tree_meta_accepts_valid_and_rejects_malformed() -> None:
    """``_parse_ls_tree_meta`` must cover valid meta and defensive None edges."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _parse_ls_tree_meta,
    )

    assert _parse_ls_tree_meta("100644 blob abc") == ("100644", "blob", "abc")
    assert _parse_ls_tree_meta("100644") is None  # missing type/oid spaces
    assert _parse_ls_tree_meta("100644 blob ") is None  # empty oid
    assert _parse_ls_tree_meta("100644 blob abc def") is None  # oid containing a space


@pytest.mark.unit
def test_git_mode_file_kind_maps_regular_symlink_gitlink_and_unknown() -> None:
    """File-kind mapping must collapse chmod bits and keep symlink/gitlink distinct."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _git_mode_file_kind,
    )

    assert _git_mode_file_kind("100644") == "file"
    assert _git_mode_file_kind("100755") == "file"
    assert _git_mode_file_kind("120000") == "symlink"
    assert _git_mode_file_kind("160000") == "gitlink"
    assert _git_mode_file_kind("040000") == "040000"


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
async def test_commit_changes_present_in_head_rejects_partial_multi_path_revert(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """One surviving salvage path must not reuse evidence after another is reverted.

    Salvage touched a.txt (the fix) and b.txt; a later tip restores only a.txt to
    the parent blob while leaving b.txt. Complete-delta presence must fail closed
    so a no-change retry cannot resolve the original thread on collateral alone.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "a.txt").write_text("base-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("base-b\n", encoding="utf-8")
    _git(repo, "add", "a.txt", "b.txt")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.txt").write_text("salvaged-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("salvaged-b\n", encoding="utf-8")
    _git(repo, "add", "a.txt", "b.txt")
    _git(repo, "commit", "-qm", "salvage both paths")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.txt").write_text("base-a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "revert only a.txt")
    partial = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=partial,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_third_content_overwrite(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Salvaged blob B must remain; differing from parent A alone is not enough.

    Salvage changes a path A→B; a later tip overwrites to C. C≠A would pass a
    parent-blob-only check even though B is gone — fail closed so a no-change
    FIXED retry cannot reuse the stale salvage (PRRT_kwDOSJAM6s6Zl8_a).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "a.txt").write_text("base-a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.txt").write_text("salvaged-b\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "salvage A to B")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.txt").write_text("later-c\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "overwrite B with C")
    third_content = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=third_content,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_accepts_same_file_later_hunk(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Same-file non-overlapping later hunk must retain salvage evidence.

    Salvage edits one middle line; a later tip edits a different line so the
    blob OID differs while the salvaged line remains. Overwriting or reverting
    that salvaged line must still fail closed (PRRT_kwDOSJAM6s6ZmWRh).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "a.py").write_text(
        "line1\nline2\nline3-middle\nline4\nline5-other\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "base multi-line")
    (repo / "a.py").write_text(
        "line1\nline2\nline3-salvaged\nline4\nline5-other\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "salvage middle hunk")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.py").write_text(
        "line1\nline2\nline3-salvaged\nline4\nline5-later\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "later tip different hunk")
    later_hunk = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Overwrite the salvaged middle line with third content (keep later hunk).
    (repo / "a.py").write_text(
        "line1\nline2\nline3-third\nline4\nline5-later\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "overwrite salvaged hunk")
    third_content = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Revert only the salvaged middle line back to the parent spelling.
    _git(repo, "checkout", "-q", "-B", "revert-salvage-hunk", later_hunk)
    (repo / "a.py").write_text(
        "line1\nline2\nline3-middle\nline4\nline5-later\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "revert salvaged hunk")
    reverted_hunk = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=later_hunk,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=third_content,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=reverted_hunk,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_accepts_addition_later_edit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """No-baseline file addition must retain when later tips keep added bytes.

    Salvage adds a new path. A later tip that appends (or prepends) changes the
    blob OID while the original added content remains contiguous; exact-OID
    equality would discard that salvage and block a no-change FIXED retry
    (PRRT_kwDOSJAM6s6Zm0PC). Overwriting or deleting the added bytes must still
    fail closed.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.py").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.py")
    _git(repo, "commit", "-qm", "base without new file")

    (repo / "new.py").write_text(
        "line1-added\nline2-salvaged\nline3-added\n",
        encoding="utf-8",
    )
    _git(repo, "add", "new.py")
    _git(repo, "commit", "-qm", "salvage adds new.py")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "new.py").write_text(
        "line1-added\nline2-salvaged\nline3-added\nline4-appended\n",
        encoding="utf-8",
    )
    _git(repo, "add", "new.py")
    _git(repo, "commit", "-qm", "later tip appends to addition")
    appended = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-B", "prepend-tip", salvage)
    (repo / "new.py").write_text(
        "line0-prepended\nline1-added\nline2-salvaged\nline3-added\n",
        encoding="utf-8",
    )
    _git(repo, "add", "new.py")
    _git(repo, "commit", "-qm", "later tip prepends to addition")
    prepended = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-B", "overwrite-tip", salvage)
    (repo / "new.py").write_text("completely-different\n", encoding="utf-8")
    _git(repo, "add", "new.py")
    _git(repo, "commit", "-qm", "overwrite added content")
    overwritten = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-B", "delete-tip", salvage)
    _git(repo, "rm", "-q", "new.py")
    _git(repo, "commit", "-qm", "delete added file")
    deleted = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=appended,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=prepended,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=overwritten,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=deleted,
    )


@pytest.mark.unit
def test_added_salvage_blob_retained_rejects_mid_line_modified_occurrence() -> None:
    """Commenting out an added call must not count as retained salvage bytes.

    ``enable_guard()\\n`` is a contiguous substring of ``# enable_guard()\\n``, so
    raw containment would reuse stale addition evidence after the functional call
    was disabled (PRRT_kwDOSJAM6s6Zm6F1).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _added_salvage_blob_retained,
    )

    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="enable_guard()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="enable_guard()\nextra\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="prefix\nenable_guard()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="# enable_guard()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard()",
        head_blob="x_enable_guard()",
    )
    assert _added_salvage_blob_retained(commit_blob="", head_blob="anything\n")


@pytest.mark.unit
def test_bytes_unsafe_for_text_merge_distinguishes_intentional_fffd() -> None:
    """Strict UTF-8 / NUL gate must allow intentional U+FFFD, reject invalid bytes."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _bytes_unsafe_for_text_merge,
        _merge_file_result_matches_head,
        _raw_blob_from_cat_file_result,
    )

    intentional = "keep\ufffdsafe\n".encode("utf-8")
    assert not _bytes_unsafe_for_text_merge(intentional)
    assert not _bytes_unsafe_for_text_merge(b"plain ascii\n")
    assert _bytes_unsafe_for_text_merge(b"has\0nul\n")
    assert _bytes_unsafe_for_text_merge(b"bad-\xff\n")

    assert _raw_blob_from_cat_file_result(ok=False, stdout="", stdout_bytes=None) is None
    assert (
        _raw_blob_from_cat_file_result(ok=True, stdout="ignored", stdout_bytes=intentional)
        == intentional
    )
    assert (
        _raw_blob_from_cat_file_result(ok=True, stdout="plain\n", stdout_bytes=None) == b"plain\n"
    )
    assert _raw_blob_from_cat_file_result(ok=True, stdout="has\ufffd", stdout_bytes=None) is None
    assert _raw_blob_from_cat_file_result(ok=True, stdout="has\0", stdout_bytes=None) is None

    assert _merge_file_result_matches_head(
        head_raw=intentional, stdout="ignored", stdout_bytes=intentional
    )
    assert _merge_file_result_matches_head(head_raw=b"plain\n", stdout="plain\n", stdout_bytes=None)
    assert not _merge_file_result_matches_head(
        head_raw=b"plain\n", stdout="other\n", stdout_bytes=None
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_commented_out_addition(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Later tip that comments out an added salvage call must fail closed.

    Salvage adds ``enable_guard()``. A subsequent edit to ``# enable_guard()``
    still contains the salvage bytes mid-line; substring retention would reuse
    stale evidence on a no-change FIXED retry (PRRT_kwDOSJAM6s6Zm6F1).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.py").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.py")
    _git(repo, "commit", "-qm", "base without new file")

    (repo / "guard.py").write_text("enable_guard()\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "salvage adds enable_guard")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "guard.py").write_text("# enable_guard()\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "later tip comments out addition")
    commented = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: append after the added call keeps a line-aligned salvage block.
    _git(repo, "checkout", "-q", "-B", "append-tip", salvage)
    (repo / "guard.py").write_text("enable_guard()\nextra()\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "later tip appends after addition")
    appended = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=appended,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=commented,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_accepts_intentional_fffd_later_hunk(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Intentional U+FFFD in valid UTF-8 must not block same-file later-hunk retention.

    Gating on the decoded replacement character rejects legitimate ``\\ufffd``
    source bytes as if they were ``decode(errors="replace")`` artifacts, so a
    later tip that edits another hunk while keeping the salvage fix falsely
    fails closed (PRRT_kwDOSJAM6s6ZnK_D).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    fffd = "\ufffd"
    (repo / "a.py").write_text(
        f"line1{fffd}keep\nline2\nline3-middle\nline4\nline5-other\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "base with intentional replacement char")
    (repo / "a.py").write_text(
        f"line1{fffd}keep\nline2\nline3-salvaged\nline4\nline5-other\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "salvage middle hunk")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.py").write_text(
        f"line1{fffd}keep\nline2\nline3-salvaged\nline4\nline5-later\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "later tip different hunk")
    later_hunk = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.py").write_text(
        f"line1{fffd}keep\nline2\nline3-third\nline4\nline5-later\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "overwrite salvaged hunk")
    third_content = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=later_hunk,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=third_content,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_invalid_utf8_replace_collapse(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Distinct invalid-UTF-8 blobs must not retain salvage via U+FFFD collapse.

    ``AsyncCommandRunner`` decodes cat-file with ``errors="replace"``. Parent,
    salvage, overwrite, and revert blobs that differ only in invalid bytes all
    become the same replacement-character text, so merge-file would falsely prove
    retention unless invalid UTF-8 raw bytes fail closed (exact OID only).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")

    # Three distinct invalid sequences that all decode to the same U+FFFD text.
    (repo / "blob.bin").write_bytes(b"payload-\xff\n")
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-qm", "base invalid utf-8")
    (repo / "blob.bin").write_bytes(b"payload-\xfe\n")
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-qm", "salvage different invalid byte")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "blob.bin").write_bytes(b"payload-\xfd\n")
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-qm", "overwrite with third invalid byte")
    third_invalid = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-B", "revert-invalid", salvage)
    (repo / "blob.bin").write_bytes(b"payload-\xff\n")
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-qm", "revert to parent invalid bytes")
    reverted_invalid = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: exact salvage OID at HEAD still retains (early OID equality).
    _git(repo, "checkout", "-q", "-B", "exact-salvage", salvage)
    (repo / "other.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "unrelated while salvage OID preserved")
    preserved_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=preserved_oid,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=third_invalid,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=reverted_invalid,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_newline_pathname_overwrite(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Newline pathnames must use -z bytes; C-quoted spellings must not retain salvage.

    Without ``diff-tree -z``, Git emits a C-quoted path for names containing a
    newline. ``splitlines()`` feeds that spelling to ``ls-tree``; both lookups
    return empty and compare equal, so a later overwrite/revert falsely looks
    present (PRRT_kwDOSJAM6s6ZmCZz).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "commit", "-qm", "base")

    weird_name = "weird\nname.txt"
    (repo / weird_name).write_text("salvaged\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "salvage newline pathname")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: later tip keeps the salvage entry while adding an unrelated path.
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "unrelated while salvage preserved")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Overwrite/remove the newline pathname so salvage content is gone.
    _git(repo, "rm", "-f", "--", weird_name)
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "remove weird pathname")
    removed = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=removed,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_retains_invalid_utf8_pathname(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Invalid-UTF-8 pathnames must survive runner decode for salvage retention.

    ``AsyncioSubprocessRunner`` decodes ``diff-tree -z`` with ``errors="replace"``,
    so a legal Git pathname containing ``\\xff`` becomes a different U+FFFD spelling.
    ``ls-tree`` then misses every lookup and valid salvage is discarded
    (PRRT_kwDOSJAM6s6ZmviP). Path records must be taken from raw stdout bytes.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "commit", "-qm", "base")

    weird_name = b"bad-\xff-name.txt"
    (repo / os.fsdecode(weird_name)).write_text("salvaged\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "salvage invalid utf-8 pathname"],
        check=True,
        capture_output=True,
    )
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "unrelated while invalid pathname salvage preserved")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    subprocess.run(
        ["git", "-C", str(repo), "--literal-pathspecs", "rm", "-f", "--", weird_name],
        check=True,
        capture_output=True,
    )
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "remove invalid utf-8 pathname")
    removed = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=removed,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_pathspec_magic_filename_revert(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Diff-derived paths must use ``--literal-pathspecs`` for ``ls-tree``.

    A legal filename such as ``:(literal)foo`` is pathspec magic without the
    global option: ``ls-tree`` reads ``foo`` instead. After reverting the
    magic-named file while leaving ``foo`` unchanged, baseline/salvage/HEAD
    lookups all return ``foo``'s identical entry, so salvage falsely retains
    (PRRT_kwDOSJAM6s6ZmirW).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "commit", "-qm", "base")

    magic_name = ":(literal)foo"
    (repo / magic_name).write_text("magic-salvage\n", encoding="utf-8")
    (repo / "foo").write_text("normal\n", encoding="utf-8")
    _git(repo, "--literal-pathspecs", "add", "-A")
    _git(repo, "commit", "-qm", "salvage magic pathname and foo")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: later tip keeps the magic path while adding an unrelated file.
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "unrelated while magic salvage preserved")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Revert only the magic-named path; leave ``foo`` byte-identical so a
    # non-literal ls-tree would still see matching foo entries.
    _git(repo, "--literal-pathspecs", "rm", "-f", "--", magic_name)
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "revert magic pathname leave foo")
    reverted = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=reverted,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_accepts_preserved_deletion(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Deletion salvage must reuse when the tip still lacks the deleted path.

    A crashed fix that removed a file leaves an empty salvage tree entry. Later
    tips that preserve that absence must retain evidence; a tip that recreates
    the file must fail closed (PRRT_kwDOSJAM6s6ZmEAd).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    (repo / "gone.txt").write_text("delete-me\n", encoding="utf-8")
    _git(repo, "add", "keep.txt", "gone.txt")
    _git(repo, "commit", "-qm", "base with file to delete")
    _git(repo, "rm", "-q", "gone.txt")
    (repo / "keep.txt").write_text("keep-and-edit\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "commit", "-qm", "salvage deletes gone.txt and edits keep.txt")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "later.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "later tip preserving deletion")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "gone.txt").write_text("recreated\n", encoding="utf-8")
    _git(repo, "add", "gone.txt")
    _git(repo, "commit", "-qm", "undo deletion")
    recreated = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=recreated,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_accepts_preserved_newline_deletion(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """NUL-parsed newline pathnames must retain deletion salvage like plain paths.

    Salvage deletes a legal pathname containing a newline. A later tip that keeps
    the path absent must reuse evidence; recreating the file must fail closed
    (operator hint op_c7b81dcfeeda494596a261f7 / PRRT_kwDOSJAM6s6ZmEAd).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    weird_name = "weird\nname.txt"
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    (repo / weird_name).write_text("delete-me\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base with newline pathname")
    _git(repo, "rm", "-q", "--", weird_name)
    _git(repo, "commit", "-qm", "salvage deletes newline pathname")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "later.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "later tip preserving newline deletion")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / weird_name).write_text("recreated\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "re-add newline pathname")
    readded = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=readded,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_both_missing_tree_entries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Two empty ls-tree tokens must not count as retained salvage evidence.

    Absence on the salvage tip is only a legitimate deletion when the parent
    still had the path. A bogus/C-quoted spelling that misses parent, salvage,
    and head alike must fail closed.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    commit = "1" * 40
    head = "2" * 40
    commit_tree = "a" * 40
    head_tree = "b" * 40
    parent = "3" * 40
    parent_tree = "c" * 40
    # C-quoted-style path that will miss in both trees when looked up as-is.
    bogus_path = '"weird\\nname.txt"'

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{commit_tree}\n")  # commit^{tree}
    cmd.queue_result(returncode=0, stdout=f"{head_tree}\n")  # head^{tree}
    cmd.queue_result(returncode=0, stdout=f"{parent}\n")  # commit^
    cmd.queue_result(returncode=0, stdout=f"{parent_tree}\n")  # parent^{tree}
    cmd.queue_result(returncode=0, stdout=f"{bogus_path}\0")  # diff-tree -z
    cmd.queue_result(returncode=0, stdout="")  # ls-tree parent (missing)
    cmd.queue_result(returncode=0, stdout="")  # ls-tree commit (missing)
    cmd.queue_result(returncode=0, stdout="")  # ls-tree head (missing)

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
        commit=commit,
        head=head,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_symlink_kind_swap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Content-only salvage must not retain when HEAD swaps file→symlink.

    Salvage writes pathname bytes into a regular file. A later tip replaces that
    file with a symlink to the same path: Git stores both as type blob with the
    same OID. Skipping mode equality for content-only salvage must still reject
    the kind change so a no-change FIXED retry cannot reuse stale evidence
    (PRRT_kwDOSJAM6s6Znm-O).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    path = repo / "linkish"
    path.write_text("other\n", encoding="utf-8")
    _git(repo, "add", "linkish")
    _git(repo, "commit", "-qm", "base regular file")
    path.write_text("target", encoding="utf-8")
    _git(repo, "add", "linkish")
    _git(repo, "commit", "-qm", "salvage content pathname")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "rm", "-q", "linkish")
    path.symlink_to("target")
    _git(repo, "add", "linkish")
    (repo / "unrelated.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-qm", "replace with symlink and add unrelated")
    kind_swapped = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: keep regular-file kind while adding an unrelated path — still present.
    _git(repo, "checkout", "-q", "-B", "kind-preserved", salvage)
    (repo / "other.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "unrelated while kind preserved")
    kind_preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: content-only salvage still tolerates same-kind chmod on a later tip.
    _git(repo, "checkout", "-q", "-B", "chmod-later", salvage)
    _git(repo, "update-index", "--chmod=+x", "linkish")
    (repo / "chmod-extra.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "linkish", "chmod-extra.txt")
    _git(repo, "commit", "-qm", "chmod +x while content retained")
    chmod_later = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=kind_preserved,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=chmod_later,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=kind_swapped,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_mode_only_revert(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Mode/type are part of salvage evidence; blob OID alone must not retain it.

    Salvage only makes a script executable. A later tip reverts that mode while
    adding an unrelated path: parent/head trees differ (so the full-tree shortcut
    does not reject) and blob OIDs still match. Complete tree-entry comparison
    must fail closed (PRRT_kwDOSJAM6s6Zl_za).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    script = repo / "script.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    _git(repo, "add", "script.sh")
    _git(repo, "commit", "-qm", "base non-executable")
    _git(repo, "update-index", "--chmod=+x", "script.sh")
    _git(repo, "commit", "-qm", "salvage make executable")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "update-index", "--chmod=-x", "script.sh")
    (repo / "unrelated.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "script.sh", "unrelated.txt")
    _git(repo, "commit", "-qm", "revert mode and add unrelated")
    mode_reverted = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: keep executable mode while adding an unrelated path — still present.
    _git(repo, "checkout", "-q", "-B", "mode-preserved", salvage)
    (repo / "other.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "unrelated while mode preserved")
    mode_preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

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
        head=mode_preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=mode_reverted,
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


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_earlier_multi_commit_revert(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Multi-commit salvage must verify start..tip, not only tip^..tip.

    A failed run that creates H1 (review fix) then H2 (unrelated) retains H2. The
    first-parent delta is only H1..H2. A later tip that reverts H1 while preserving
    H2 must fail closed when ``baseline`` is the invocation start SHA
    (PRRT_kwDOSJAM6s6ZmG-B).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "a.txt").write_text("base-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("base-b\n", encoding="utf-8")
    _git(repo, "add", "a.txt", "b.txt")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.txt").write_text("fix-a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "H1 review fix")
    h1 = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "b.txt").write_text("unrelated-b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "H2 unrelated")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Later tip: revert H1's fix, keep H2's unrelated change.
    (repo / "a.txt").write_text("base-a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "revert H1 keep H2")
    later = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: later tip that preserves both H1 and H2 deltas.
    _git(repo, "checkout", "-q", "-B", "both-preserved", salvage)
    (repo / "c.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "preserve full start..salvage")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert h1 != salvage
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
        baseline=start,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=preserved,
        baseline=start,
    )
    # First-parent-only check would still see H2's b.txt and wrongly return True.
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=later,
        baseline=start,
    )
