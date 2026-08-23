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

    # Mark the fixture repo before poisoning GIT_* so init/commit do not inherit
    # a broken object directory (CI: git commit SIGABRT under GIT_OBJECT_DIRECTORY).
    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)

    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    monkeypatch.setenv("GIT_GRAFT_FILE", "/tmp/agent-grafts")
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/replace")
    # A poisoned host env must not leave replace objects enabled.
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)

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
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".github/workflows/ci.yml", ".github/workflows/ci.yml"),
        ("./.github/workflows/ci.yml", ".github/workflows/ci.yml"),
        ("./src/target.py", "src/target.py"),
        ("src/target.py", "src/target.py"),
        ("  ./.env  ", ".env"),
        (".coveragerc", ".coveragerc"),
        ("github/workflows/ci.yml", "github/workflows/ci.yml"),
    ],
)
def test_normalize_evidence_item_path_preserves_dotfiles(raw: str, expected: str) -> None:
    """Strip only exact ``./`` prefixes; keep leading dots on real dotfile paths.

    PRRT_kwDOSJAM6s6Z0nDa: ``lstrip("./")`` collapses ``.github/...`` into
    ``github/...``, letting an unrelated non-dot path satisfy the FIXED gate.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _normalize_evidence_item_path,
    )

    assert _normalize_evidence_item_path(raw) == expected


@pytest.mark.unit
async def test_commit_range_touches_path_does_not_confuse_dotfile_with_non_dot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A non-dot path change must not satisfy a ``.github/...`` review item path."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "github" / "workflows" / "ci.yml").write_text("non-dot\n", encoding="utf-8")
    (repo / ".github" / "workflows" / "ci.yml").write_text("dotfile\n", encoding="utf-8")
    _git(repo, "add", "github/workflows/ci.yml", ".github/workflows/ci.yml")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "github" / "workflows" / "ci.yml").write_text("non-dot changed\n", encoding="utf-8")
    _git(repo, "add", "github/workflows/ci.yml")
    _git(repo, "commit", "-qm", "non-dot only")
    non_dot_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=non_dot_tip,
        path=".github/workflows/ci.yml",
    )
    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=non_dot_tip,
        path="github/workflows/ci.yml",
    )


@pytest.mark.unit
def test_changed_path_in_item_scope_accepts_same_directory_cross_file() -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    assert pre_push_validation._changed_path_in_item_scope(
        item_path="src/awf/reviewed.py",
        changed_path="src/awf/helper.py",
    )
    # PRRT_kwDOSJAM6s6bbZlt: unrelated paths never count, even when workspace owns them.
    assert not pre_push_validation._changed_path_in_item_scope(
        item_path="src/target.py",
        changed_path="README.md",
    )
    # PRRT_kwDOSJAM6s6bbkfx: root-level siblings must not satisfy same-parent fallback.
    assert not pre_push_validation._changed_path_in_item_scope(
        item_path="pyproject.toml",
        changed_path="README.md",
    )
    # PRRT_kwDOSJAM6s6bb9qc: literal Git paths with [, *, or ? are not owned-path globs.
    assert not pre_push_validation._changed_path_in_item_scope(
        item_path="src[old]/target.py",
        changed_path="src_new/unrelated.py",
    )


@pytest.mark.unit
async def test_commit_range_in_item_scope_requires_related_delta(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """FIXED evidence for an anchored thread must reject unrelated README-only deltas."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    (repo / "src" / "target.py").write_text("v1\n", encoding="utf-8")
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    _git(repo, "add", "src/target.py", "README.md")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "README.md").write_text("docs unrelated\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "readme only")
    readme_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "src" / "target.py").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "item path")
    item_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_in_item_scope(
        runner,
        worktree_path=repo,
        left=start,
        right=readme_tip,
        item_path="src/target.py",
    )
    assert await pre_push_validation._commit_range_in_item_scope(
        runner,
        worktree_path=repo,
        left=start,
        right=item_tip,
        item_path="src/target.py",
    )


@pytest.mark.unit
async def test_commit_range_touches_path_requires_item_path_in_delta(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """FIXED path evidence must require the review path in the start..end delta.

    PRRT_kwDOSJAM6s6Zzwl0: an unrelated README edit is not item evidence for a
    different reviewed file.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    (repo / "src" / "target.py").write_text("v1\n", encoding="utf-8")
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    _git(repo, "add", "src/target.py", "README.md")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "README.md").write_text("docs unrelated\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "readme only")
    readme_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "src" / "target.py").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "item path")
    item_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=readme_tip,
        path="src/target.py",
    )
    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=item_tip,
        path="src/target.py",
    )
    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=item_tip,
        path="./src/target.py",
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=item_tip,
        path="   ",
    )


@pytest.mark.unit
def test_map_review_line_through_diff_shifts_anchor_after_top_insert() -> None:
    """PRRT_kwDOSJAM6s6bdOXq: later items must not reuse cycle-start line numbers."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    top_insert = "@@ -1,0 +1,5 @@\n"
    # PRRT_kwDOSJAM6s6bdlxB: line-1 anchors must shift for top-of-file inserts.
    assert pre_push_validation._map_review_line_through_diff(1, top_insert) == 6
    assert pre_push_validation._map_review_line_through_diff(10, top_insert) == 15

    insert_before_anchor = "@@ -174,0 +175,5 @@\n"
    # PRRT_kwDOSJAM6s6bdSlA: insert-before keeps old_start unmoved.
    assert pre_push_validation._map_review_line_through_diff(174, insert_before_anchor) == 174
    assert pre_push_validation._map_review_line_through_diff(175, insert_before_anchor) == 180

    unchanged_tail = "@@ -5,1 +5,1 @@\n"
    assert pre_push_validation._map_review_line_through_diff(10, unchanged_tail) == 10

    multi_hunk = "@@ -1,0 +1,10 @@\n@@ -99,0 +110,5 @@\n"
    # PRRT_kwDOSJAM6s6bdWnC: later pure-insert hunks must use old_start, not new_start.
    assert pre_push_validation._map_review_line_through_diff(100, multi_hunk) == 115


@pytest.mark.unit
async def test_commit_range_touches_path_maps_review_line_after_earlier_item_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Later inline threads in the same file must relocate anchors through prior commits."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    target = repo / "src" / "target.py"
    target.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "cycle start")
    cycle_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    inserted = ["# earlier item line"] + target.read_text(encoding="utf-8").splitlines()
    target.write_text("\n".join(inserted) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "earlier item")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    lines = target.read_text(encoding="utf-8").splitlines()
    lines[5] = "    return value"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "anchored fix")
    anchored_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    target.write_text(
        "\n".join(
            [
                "# earlier item line",
                "def helper():",
                "    return 2",
                "",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "displaced stale line")
    displaced_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    mapped_line = await pre_push_validation._map_review_line_through_commits(
        runner,
        worktree_path=repo,
        anchor_head=cycle_start,
        target_head=item_start,
        path="src/target.py",
        line=5,
    )
    assert mapped_line == 6

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=anchored_tip,
        path="src/target.py",
        line=mapped_line,
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=displaced_tip,
        path="src/target.py",
        line=mapped_line,
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=anchored_tip,
        path="src/target.py",
        line=5,
    )


@pytest.mark.unit
async def test_rename_map_in_commit_range_honors_diff_renames_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bd59g: rename edges must not depend on diff.renames config."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    _git(repo, "config", "diff.renames", "false")
    (repo / "src").mkdir()
    (repo / "src" / "old.py").write_text("reviewed\n", encoding="utf-8")
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "mv", "src/old.py", "src/new.py")
    _git(repo, "commit", "-qm", "rename only")
    rename_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    mapped_path = await pre_push_validation._map_review_path_through_commits(
        runner,
        worktree_path=repo,
        anchor_head=start,
        target_head=rename_tip,
        path="src/old.py",
    )
    assert mapped_path == "src/new.py"


@pytest.mark.unit
async def test_map_review_anchor_carries_rename_target_for_fixed_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bdpo_: rename targets must follow line anchors across items."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_path = repo / "src" / "old.py"
    old_path.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "cycle start")
    cycle_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "mv", "src/old.py", "src/new.py")
    _git(repo, "commit", "-qm", "earlier item rename")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    new_path = repo / "src" / "new.py"
    lines = new_path.read_text(encoding="utf-8").splitlines()
    lines[4] = "    return value"
    new_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "anchored fix")
    anchored_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    mapped_line = await pre_push_validation._map_review_line_through_commits(
        runner,
        worktree_path=repo,
        anchor_head=cycle_start,
        target_head=item_start,
        path="src/old.py",
        line=5,
    )
    assert mapped_line == 5

    mapped_path = await pre_push_validation._map_review_path_through_commits(
        runner,
        worktree_path=repo,
        anchor_head=cycle_start,
        target_head=item_start,
        path="src/old.py",
    )
    assert mapped_path == "src/new.py"

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=anchored_tip,
        path=mapped_path,
        line=mapped_line,
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=anchored_tip,
        path="src/old.py",
        line=mapped_line,
    )


@pytest.mark.unit
async def test_map_review_anchor_carries_rename_target_for_file_level_fixed_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bd81Y: file-level threads must follow rename targets across items."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_path = repo / "src" / "old.py"
    old_path.write_text("reviewed module\n", encoding="utf-8")
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "cycle start")
    cycle_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "mv", "src/old.py", "src/new.py")
    _git(repo, "commit", "-qm", "earlier item rename")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    new_path = repo / "src" / "new.py"
    new_path.write_text("reviewed module fixed\n", encoding="utf-8")
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "file-level fix")
    fixed_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    mapped_path = await pre_push_validation._map_review_path_through_commits(
        runner,
        worktree_path=repo,
        anchor_head=cycle_start,
        target_head=item_start,
        path="src/old.py",
    )
    assert mapped_path == "src/new.py"

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=fixed_tip,
        path=mapped_path,
        line=None,
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=fixed_tip,
        path="src/old.py",
        line=None,
    )


@pytest.mark.unit
async def test_invoke_cli_for_verdict_maps_file_level_rename_path_for_fixed_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6bd81Y: file-level FIXED must accept edits on renamed paths."""
    from awf.adapters.base import AgentRunResult
    from awf.runtime.pr_monitor_runner import comment_verdict

    worktree = tmp_path / "worktrees" / "ws_protocol"
    worktree.mkdir(parents=True)
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "src").mkdir()
    old_path = worktree / "src" / "old.py"
    old_path.write_text("reviewed module\n", encoding="utf-8")
    _git(worktree, "add", "src/old.py")
    _git(worktree, "commit", "-qm", "cycle start")
    cycle_start = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    _git(worktree, "mv", "src/old.py", "src/new.py")
    _git(worktree, "commit", "-qm", "earlier item rename")
    item_start = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    new_path = worktree / "src" / "new.py"
    new_path.write_text("reviewed module fixed\n", encoding="utf-8")
    _git(worktree, "add", "src/new.py")
    _git(worktree, "commit", "-qm", "file-level fix")
    fixed_tip = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)

    async def _fixed_agent(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: updated renamed module",
            stderr="",
        )

    runner._run_monitor_agent_with_service_recovery = _fixed_agent

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_protocol",
        prompt="fix the reviewed module",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=item_start,
        evidence_item_path="src/old.py",
        evidence_item_line=None,
        evidence_anchor_head=cycle_start,
        commit_dirty_changes=False,
    )

    assert result.verdict == "fix_committed"
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == fixed_tip


@pytest.mark.unit
async def test_commit_range_touches_path_uses_rename_aware_diff_for_current_item(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bd3lp: pure rename must not satisfy anchored line evidence."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_path = repo / "src" / "old.py"
    old_path.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "mv", "src/old.py", "src/new.py")
    _git(repo, "commit", "-qm", "pure rename only")
    rename_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    new_path = repo / "src" / "new.py"
    lines = new_path.read_text(encoding="utf-8").splitlines()
    lines[1] = "    return 2"
    new_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "unrelated edit elsewhere")
    unrelated_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    lines = new_path.read_text(encoding="utf-8").splitlines()
    lines[4] = "    return value"
    new_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "anchored fix after rename")
    anchored_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rename_tip,
        path="src/old.py",
        line=5,
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=unrelated_tip,
        path="src/old.py",
        line=5,
    )
    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=anchored_tip,
        path="src/old.py",
        line=5,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_honors_diff_renames_false_for_content_diff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bd_Zr: content diffs must force rename detection when config disables it."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    _git(repo, "config", "diff.renames", "false")
    (repo / "src").mkdir()
    old_path = repo / "src" / "old.py"
    old_path.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "mv", "src/old.py", "src/new.py")
    _git(repo, "commit", "-qm", "pure rename only")
    rename_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    mapped_line = await pre_push_validation._map_review_line_through_commits(
        runner,
        worktree_path=repo,
        anchor_head=item_start,
        target_head=rename_tip,
        path="src/old.py",
        line=5,
    )
    assert mapped_line == 5

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rename_tip,
        path="src/old.py",
        line=5,
    )


@pytest.mark.unit
def test_path_deletion_addition_without_rename_detects_unpaired_delete_add() -> None:
    """PRRT_kwDOSJAM6s6beOKJ: D+A without R must not count as rename evidence."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    unpaired = "D\0src/old.py\0A\0src/new.py\0"
    assert pre_push_validation._path_deletion_addition_without_rename(unpaired, "src/old.py")
    assert not pre_push_validation._path_deletion_addition_without_rename(unpaired, "src/other.py")

    unrelated_add = "D\0src/old.py\0A\0tests/test_foo.py\0"
    assert not pre_push_validation._path_deletion_addition_without_rename(
        unrelated_add, "src/old.py"
    )

    test_prefix_rename = "D\0src/foo.py\0A\0tests/test_foo.py\0"
    assert pre_push_validation._path_deletion_addition_without_rename(
        test_prefix_rename, "src/foo.py"
    )

    colocated_test = "D\0src/old.py\0A\0src/test_old.py\0"
    assert not pre_push_validation._path_deletion_addition_without_rename(
        colocated_test, "src/old.py"
    )

    colocated_module_test = "D\0src/module.py\0A\0src/module_test.py\0"
    assert not pre_push_validation._path_deletion_addition_without_rename(
        colocated_module_test, "src/module.py"
    )

    # PRRT_kwDOSJAM6s6bfPjA: fixtures->conftest is a plausible below-threshold rename.
    fixtures_to_conftest = "D\0src/fixtures.py\0A\0src/conftest.py\0"
    assert pre_push_validation._path_deletion_addition_without_rename(
        fixtures_to_conftest, "src/fixtures.py"
    )

    root_rename = "D\0foo.py\0A\0bar.py\0"
    assert pre_push_validation._path_deletion_addition_without_rename(root_rename, "foo.py")

    cross_dir_basename = "D\0src/foo.py\0A\0lib/foo.py\0"
    assert pre_push_validation._path_deletion_addition_without_rename(
        cross_dir_basename, "src/foo.py"
    )

    cross_dir_cross_name = "D\0src/old.py\0A\0lib/new.py\0"
    assert pre_push_validation._path_deletion_addition_without_rename(
        cross_dir_cross_name, "src/old.py"
    )

    # PRRT_kwDOSJAM6s6bfEkW: same-basename moves into tests/ stay plausible renames.
    cross_dir_into_tests = "D\0src/tests/foo.py\0A\0tests/foo.py\0"
    assert pre_push_validation._path_deletion_addition_without_rename(
        cross_dir_into_tests, "src/tests/foo.py"
    )

    rename_edge = "R014\0src/old.py\0src/new.py\0"
    assert not pre_push_validation._path_deletion_addition_without_rename(rename_edge, "src/old.py")

    delete_only = "D\0src/old.py\0"
    assert not pre_push_validation._path_deletion_addition_without_rename(delete_only, "src/old.py")


@pytest.mark.unit
def test_plausible_rename_partners_for_deletion_lists_only_plausible_adds() -> None:
    """PRRT_kwDOSJAM6s6bfThO: partner lookup must ignore unrelated same-dir adds."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _plausible_rename_partners_for_deletion,
    )

    fixtures_to_utils_and_conftest = "D\0src/fixtures.py\0A\0src/utils.py\0A\0src/conftest.py\0"
    assert _plausible_rename_partners_for_deletion(
        fixtures_to_utils_and_conftest,
        "src/fixtures.py",
    ) == ("src/utils.py", "src/conftest.py")

    unrelated_test_add = "D\0src/old.py\0A\0tests/test_foo.py\0"
    assert _plausible_rename_partners_for_deletion(unrelated_test_add, "src/old.py") == ()

    test_prefix_rename = "D\0src/foo.py\0A\0tests/test_foo.py\0"
    assert _plausible_rename_partners_for_deletion(test_prefix_rename, "src/foo.py") == (
        "tests/test_foo.py",
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_test_prefix_rename_into_tests(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfUzl: test-prefixed moves into tests/ must not bypass rename checks."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    fixtures_path = repo / "src" / "fixtures.py"
    fixtures_path.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "@pytest.fixture",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/fixtures.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = 5

    fixtures_path.unlink()
    (repo / "tests" / "test_fixtures.py").write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "# rewritten helper module",
                "@pytest.fixture",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "move fixtures helper to tests/test_fixtures")
    rewrite_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rewrite_tip,
        path="src/fixtures.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_test_prefix_rename_with_single_anchor_overlap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfhwX: one retained anchor line must block test-prefix exemption."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    reviewed_line = 110
    old_lines = (
        [f"old{i}" for i in range(1, reviewed_line)]
        + ["ANCHOR_CALL()"]
        + [f"old{i}" for i in range(reviewed_line + 1, 221)]
    )
    foo_path = repo / "src" / "foo.py"
    foo_path.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/foo.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    foo_path.unlink()
    new_lines = (
        [f"new{i}" for i in range(1, reviewed_line)]
        + ["ANCHOR_CALL()"]
        + [f"new{i}" for i in range(reviewed_line + 1, 221)]
    )
    (repo / "tests" / "test_foo.py").write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "move foo helper to tests/test_foo with bulk rewrite")
    rewrite_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rewrite_tip,
        path="src/foo.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_conftest_rewrite_with_single_anchor_overlap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfmuj: one retained anchor line must block conftest exemption."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    reviewed_line = 110
    old_lines = (
        [f"old{i}" for i in range(1, reviewed_line)]
        + ["ANCHOR_CALL()"]
        + [f"old{i}" for i in range(reviewed_line + 1, 221)]
    )
    fixtures_path = repo / "src" / "fixtures.py"
    fixtures_path.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/fixtures.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    fixtures_path.unlink()
    new_lines = (
        [f"new{i}" for i in range(1, reviewed_line)]
        + ["ANCHOR_CALL()"]
        + [f"new{i}" for i in range(reviewed_line + 1, 221)]
    )
    (repo / "src" / "conftest.py").write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rewrite fixtures helper as conftest with bulk rewrite")
    rewrite_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rewrite_tip,
        path="src/fixtures.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_allows_anchored_delete_with_colocated_test(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfLFk: colocated regression tests must not block anchored deletions."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_path = repo / "src" / "old.py"
    old_path.write_text("keep\nREVIEWED\nremove\n", encoding="utf-8")
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = 2

    old_path.unlink()
    (repo / "src" / "test_old.py").write_text("def test_old():\n    pass\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete obsolete module and add colocated regression test")
    fix_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=fix_tip,
        path="src/old.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_fixtures_to_conftest_rewrite(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfPjA: fixtures->conftest rewrites must not satisfy old-path anchors."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    fixtures_path = repo / "src" / "fixtures.py"
    fixtures_path.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "@pytest.fixture",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/fixtures.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = 5

    fixtures_path.unlink()
    (repo / "src" / "conftest.py").write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "# rewritten helper module",
                "@pytest.fixture",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rewrite fixtures helper as conftest")
    rewrite_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rewrite_tip,
        path="src/fixtures.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_allows_anchored_delete_with_unrelated_conftest(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfPjA: unrelated conftest adds must not block anchored deletions."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_path = repo / "src" / "old.py"
    old_path.write_text("keep\nREVIEWED\nremove\n", encoding="utf-8")
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = 2

    old_path.unlink()
    (repo / "src" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef fresh():\n    return None\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete obsolete module and add unrelated conftest")
    fix_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=fix_tip,
        path="src/old.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_when_unrelated_conftest_masks_rename(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfThO: unrelated conftest must not exempt other plausible D+A partners."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    fixtures_path = repo / "src" / "fixtures.py"
    fixtures_path.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "@pytest.fixture",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/fixtures.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = 5

    fixtures_path.unlink()
    (repo / "src" / "utils.py").write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "@pytest.fixture",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "src" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef fresh():\n    return None\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rewrite fixtures helper as utils plus unrelated conftest")
    rewrite_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rewrite_tip,
        path="src/fixtures.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_when_unrelated_conftest_masks_test_rename(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfUzh: unrelated conftest must not exempt below-threshold test renames."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    fixtures_path = repo / "src" / "fixtures.py"
    fixtures_path.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "@pytest.fixture",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/fixtures.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = 5

    fixtures_path.unlink()
    (repo / "tests" / "test_fixtures.py").write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "@pytest.fixture",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "src" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef fresh():\n    return None\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "move fixtures helper to tests plus unrelated conftest")
    rewrite_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rewrite_tip,
        path="src/fixtures.py",
        line=reviewed_line,
    )


@pytest.mark.unit
def test_paths_have_meaningful_line_level_content_overlap_ignores_trivial_lines() -> None:
    """PRRT_kwDOSJAM6s6bfaYk: one shared boilerplate line must not imply rename overlap."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _paths_have_meaningful_line_level_content_overlap,
    )

    deleted = {"import pytest", "def obsolete():", "return None"}
    unrelated_test = {"import pytest", "def test_old():", "pass"}
    assert not _paths_have_meaningful_line_level_content_overlap(deleted, unrelated_test)

    rewrite = {"import pytest", "@pytest.fixture", "def reviewed():", "return None"}
    assert not _paths_have_meaningful_line_level_content_overlap(deleted, rewrite)
    assert _paths_have_meaningful_line_level_content_overlap(rewrite, rewrite)


@pytest.mark.unit
async def test_paths_share_review_anchor_line_ignores_trivial_overlap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfkK3: trivial anchor text must not count as retained review content."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _paths_share_review_anchor_line,
        _unrelated_test_prefix_rename_addition,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "old.py").write_text(
        "import pytest\n\ndef obsolete():\n    return None\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "src" / "old.py").unlink()
    (repo / "tests" / "test_old.py").write_text(
        "import pytest\n\ndef test_old():\n    pass\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete obsolete module and add unrelated regression test")
    fix_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await _paths_share_review_anchor_line(
        runner,
        worktree_path=repo,
        left=item_start,
        right=fix_tip,
        left_path="src/old.py",
        right_path="tests/test_old.py",
        line=1,
    )

    d_a_name_status = "D\0src/old.py\0A\0tests/test_old.py\0"
    assert await _unrelated_test_prefix_rename_addition(
        runner,
        worktree_path=repo,
        left=item_start,
        right=fix_tip,
        deleted_path="src/old.py",
        name_status_z=d_a_name_status,
        line=1,
    )

    anchor_repo = tmp_path / "anchor_repo"
    anchor_repo.mkdir()
    _git(anchor_repo, "init", "-q")
    _git(anchor_repo, "config", "user.email", "awf@example.com")
    _git(anchor_repo, "config", "user.name", "AWF Test")
    (anchor_repo / "src").mkdir()
    (anchor_repo / "tests").mkdir()
    (anchor_repo / "src" / "old.py").write_text(
        "import pytest\nANCHOR()\n",
        encoding="utf-8",
    )
    _git(anchor_repo, "add", "src/old.py")
    _git(anchor_repo, "commit", "-qm", "item start")
    anchor_start = _git(anchor_repo, "rev-parse", "HEAD").stdout.strip()
    (anchor_repo / "src" / "old.py").unlink()
    (anchor_repo / "tests" / "test_old.py").write_text(
        "import pytest\nANCHOR()\n\ndef test_old():\n    pass\n",
        encoding="utf-8",
    )
    _git(anchor_repo, "add", "-A")
    _git(anchor_repo, "commit", "-qm", "delete with retained anchor")
    anchor_tip = _git(anchor_repo, "rev-parse", "HEAD").stdout.strip()

    assert await _paths_share_review_anchor_line(
        runner,
        worktree_path=anchor_repo,
        left=anchor_start,
        right=anchor_tip,
        left_path="src/old.py",
        right_path="tests/test_old.py",
        line=2,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_allows_anchored_delete_with_shared_boilerplate(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfaYk: shared imports must not block unrelated test exemptions."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    old_path = repo / "src" / "old.py"
    old_path.write_text(
        "import pytest\n\ndef obsolete():\n    return None\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = 3

    old_path.unlink()
    (repo / "tests" / "test_old.py").write_text(
        "import pytest\n\ndef test_old():\n    pass\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete obsolete module and add unrelated regression test")
    fix_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=fix_tip,
        path="src/old.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_allows_anchored_delete_with_unrelated_add(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6be20X: unrelated adds must not block anchored file deletions."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    old_path = repo / "src" / "old.py"
    old_path.write_text("keep\nREVIEWED\nremove\n", encoding="utf-8")
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = 2

    old_path.unlink()
    (repo / "tests" / "test_old.py").write_text("def test_old():\n    pass\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete obsolete module and add regression test")
    fix_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=fix_tip,
        path="src/old.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_below_threshold_rename(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6beOKJ: low-similarity renames must not satisfy old-path anchors."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_lines = (
        [f"old{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"old{i}" for i in range(12, 21)]
    )
    old_path = repo / "src" / "old.py"
    old_path.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = old_lines.index("REVIEWED") + 1

    _git(repo, "mv", "src/old.py", "src/new.py")
    new_lines = (
        [f"new{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"new{i}" for i in range(12, 21)]
    )
    new_path = repo / "src" / "new.py"
    new_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "low-similarity rename")
    rename_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    new_lines = new_path.read_text(encoding="utf-8").splitlines()
    new_lines[0] = "bulk rewrite unrelated"
    new_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "unrelated bulk rewrite")
    unrelated_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    new_lines = new_path.read_text(encoding="utf-8").splitlines()
    new_lines[reviewed_line - 1] = "REVIEWED fixed"
    new_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "anchored fix on renamed file")
    anchored_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rename_tip,
        path="src/old.py",
        line=reviewed_line,
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=unrelated_tip,
        path="src/old.py",
        line=reviewed_line,
    )
    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=anchored_tip,
        path="src/old.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_root_level_rename(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfHED: root-level below-threshold renames must not satisfy old-path anchors."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    old_lines = (
        [f"old{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"old{i}" for i in range(12, 21)]
    )
    old_path = repo / "foo.py"
    old_path.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "foo.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = old_lines.index("REVIEWED") + 1

    _git(repo, "mv", "foo.py", "bar.py")
    new_lines = (
        [f"new{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"new{i}" for i in range(12, 21)]
    )
    new_path = repo / "bar.py"
    new_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "bar.py")
    _git(repo, "commit", "-qm", "root-level low-similarity rename")
    rename_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rename_tip,
        path="foo.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_cross_dir_same_basename_rename(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfBxP: same-basename cross-directory below-threshold renames fail closed."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_lines = (
        [f"old{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"old{i}" for i in range(12, 21)]
    )
    old_path = repo / "src" / "foo.py"
    old_path.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/foo.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = old_lines.index("REVIEWED") + 1

    (repo / "lib").mkdir()
    _git(repo, "mv", "src/foo.py", "lib/foo.py")
    new_lines = (
        [f"new{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"new{i}" for i in range(12, 21)]
    )
    new_path = repo / "lib" / "foo.py"
    new_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "lib/foo.py")
    _git(repo, "commit", "-qm", "cross-dir same-basename low-similarity rename")
    rename_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rename_tip,
        path="src/foo.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_cross_dir_into_tests_rename(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfEkW: below-threshold moves into tests/ must not bypass rename checks."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src" / "tests").mkdir(parents=True)
    old_lines = (
        [f"old{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"old{i}" for i in range(12, 21)]
    )
    old_path = repo / "src" / "tests" / "foo.py"
    old_path.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/tests/foo.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = old_lines.index("REVIEWED") + 1

    (repo / "tests").mkdir()
    _git(repo, "mv", "src/tests/foo.py", "tests/foo.py")
    new_lines = (
        [f"new{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"new{i}" for i in range(12, 21)]
    )
    new_path = repo / "tests" / "foo.py"
    new_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "tests/foo.py")
    _git(repo, "commit", "-qm", "cross-dir into tests low-similarity rename")
    rename_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rename_tip,
        path="src/tests/foo.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_cross_dir_cross_name_rename(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6be6p8: cross-name cross-directory below-threshold renames fail closed."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_lines = (
        [f"old{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"old{i}" for i in range(12, 21)]
    )
    old_path = repo / "src" / "old.py"
    old_path.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = old_lines.index("REVIEWED") + 1

    (repo / "lib").mkdir()
    _git(repo, "mv", "src/old.py", "lib/new.py")
    new_lines = (
        [f"new{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"new{i}" for i in range(12, 21)]
    )
    new_path = repo / "lib" / "new.py"
    new_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "lib/new.py")
    _git(repo, "commit", "-qm", "cross-dir cross-name low-similarity rename")
    rename_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rename_tip,
        path="src/old.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_rename_map_merges_per_commit_edges_when_range_has_unrelated_rename(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6beU9n: unrelated range-level renames must not skip per-commit recovery."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_lines = (
        [f"old{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"old{i}" for i in range(12, 21)]
    )
    old_path = repo / "src" / "old.py"
    old_path.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
    (repo / "src" / "foo.py").write_text("helper\n", encoding="utf-8")
    _git(repo, "add", "src/old.py", "src/foo.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = old_lines.index("REVIEWED") + 1

    _git(repo, "mv", "src/old.py", "src/new.py")
    new_lines = (
        [f"new{i}" for i in range(1, 11)] + ["REVIEWED"] + [f"new{i}" for i in range(12, 21)]
    )
    new_path = repo / "src" / "new.py"
    new_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "low-similarity rename")

    _git(repo, "mv", "src/foo.py", "src/bar.py")
    _git(repo, "commit", "-qm", "unrelated high-similarity rename")
    unrelated_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    new_lines = new_path.read_text(encoding="utf-8").splitlines()
    new_lines[0] = "bulk rewrite unrelated"
    new_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "unrelated bulk rewrite")
    bulk_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    new_lines = new_path.read_text(encoding="utf-8").splitlines()
    new_lines[reviewed_line - 1] = "REVIEWED fixed"
    new_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "anchored fix on renamed file")
    anchored_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    mapped_path = await pre_push_validation._map_review_path_through_commits(
        runner,
        worktree_path=repo,
        anchor_head=item_start,
        target_head=anchored_tip,
        path="src/old.py",
    )
    assert mapped_path == "src/new.py"

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=anchored_tip,
        path="src/old.py",
        line=reviewed_line,
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=unrelated_tip,
        path="src/old.py",
        line=reviewed_line,
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=bulk_tip,
        path="src/old.py",
        line=reviewed_line,
    )


@pytest.mark.unit
async def test_rename_map_ignores_side_branch_renames_absent_from_merge_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6beqQg: ours merges must not map review paths to side-branch renames."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_path = repo / "src" / "old.py"
    old_path.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = 5

    _git(repo, "branch", "side")
    _git(repo, "checkout", "side", "-q")
    _git(repo, "mv", "src/old.py", "src/new.py")
    _git(repo, "commit", "-qm", "side rename only")
    _git(repo, "checkout", "master", "-q")
    _git(repo, "merge", "-s", "ours", "side", "-m", "ours merge keeps old path")
    merge_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    lines = old_path.read_text(encoding="utf-8").splitlines()
    lines[4] = "    return fixed"
    old_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "anchored fix on retained path")
    anchored_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    mapped_path = await pre_push_validation._map_review_path_through_commits(
        runner,
        worktree_path=repo,
        anchor_head=item_start,
        target_head=anchored_tip,
        path="src/old.py",
    )
    assert mapped_path == "src/old.py"

    mapped_line = await pre_push_validation._map_review_line_through_commits(
        runner,
        worktree_path=repo,
        anchor_head=item_start,
        target_head=anchored_tip,
        path="src/old.py",
        line=reviewed_line,
    )
    assert mapped_line == reviewed_line

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=merge_tip,
        right=anchored_tip,
        path=mapped_path,
        line=mapped_line,
    )


@pytest.mark.unit
def test_add_missing_per_commit_rename_edges_preserves_range_aggregate() -> None:
    """PRRT_kwDOSJAM6s6beYGW: incomplete per-commit edges must not clobber range maps."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _add_missing_per_commit_rename_edges,
        _follow_rename_map,
    )

    rename_map = {"src/old.py": "src/new.py"}
    per_commit_map = {
        "src/old.py": "src/mid.py",
        "src/missing.py": "src/found.py",
    }
    _add_missing_per_commit_rename_edges(rename_map, per_commit_map)
    assert _follow_rename_map("src/old.py", rename_map) == "src/new.py"
    assert _follow_rename_map("src/missing.py", rename_map) == "src/found.py"


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_zero_similarity_rename_pair(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6beOKJ: D+A rename pairs below -M01 still fail closed."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "old.py").write_text("reviewed anchor line\n", encoding="utf-8")
    _git(repo, "add", "old.py")
    _git(repo, "commit", "-qm", "item start")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reviewed_line = 1

    _git(repo, "mv", "old.py", "new.py")
    (repo / "new.py").write_text("completely different content\n", encoding="utf-8")
    _git(repo, "add", "new.py")
    _git(repo, "commit", "-qm", "zero-similarity rename")
    rename_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item_start,
        right=rename_tip,
        path="old.py",
        line=reviewed_line,
    )


@pytest.mark.unit
def test_rename_diff_preserves_line_numbers_uses_rename_aware_hunks() -> None:
    """PRRT_kwDOSJAM6s6bduAa: equal path diff lengths are not enough for pure rename."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    pure_rename = (
        "diff --git a/src/old.py b/src/new.py\n"
        "similarity index 100%\n"
        "rename from src/old.py\n"
        "rename to src/new.py\n"
    )
    assert pre_push_validation._rename_diff_preserves_line_numbers(pure_rename)

    rename_with_edits = (
        "diff --git a/src/old.py b/src/new.py\n"
        "similarity index 71%\n"
        "rename from src/old.py\n"
        "rename to src/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+# inserted above anchor\n"
        "@@ -6 +6,0 @@ def reviewed():\n"
        "-trailing\n"
    )
    assert not pre_push_validation._rename_diff_preserves_line_numbers(rename_with_edits)

    equal_length_path_diffs_old = "@@ -1,3 +0,0 @@\n-line one\n-line two\n-line three\n"
    equal_length_path_diffs_new = "@@ -0,0 +1,3 @@\n+inserted\n+line two\n+line three\n"
    combined_rename_with_edits = (
        "diff --git a/src/old.py b/src/new.py\n"
        "similarity index 71%\n"
        "rename from src/old.py\n"
        "rename to src/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+inserted\n"
        "@@ -3 +3,0 @@\n"
        "-line one\n"
    )
    assert not pre_push_validation._rename_diff_preserves_line_numbers(combined_rename_with_edits)
    # Path-filtered diffs alone cannot distinguish pure rename from equal-length edits.
    assert len(equal_length_path_diffs_old.splitlines()) == len(
        equal_length_path_diffs_new.splitlines()
    )


@pytest.mark.unit
async def test_map_review_anchor_relocates_through_rename_with_content_shift(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bdtko: rename commits that edit content must shift anchors."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    old_path = repo / "src" / "old.py"
    old_path.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                "    return None",
                "trailing",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-qm", "cycle start")
    cycle_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "mv", "src/old.py", "src/new.py")
    new_path = repo / "src" / "new.py"
    new_path.write_text(
        "\n".join(
            [
                "# inserted",
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/new.py")
    _git(repo, "commit", "-qm", "rename with content shift")
    item_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    mapped_line = await pre_push_validation._map_review_line_through_commits(
        runner,
        worktree_path=repo,
        anchor_head=cycle_start,
        target_head=item_start,
        path="src/old.py",
        line=5,
    )
    assert mapped_line == 6


@pytest.mark.unit
def test_diff_hunk_touches_line_detects_review_anchor_overlap() -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    diff_text = (
        "@@ -5,1 +5,1 @@\n"
        "-old helper\n"
        "+new helper\n"
        "@@ -42,2 +42,3 @@\n"
        " def reviewed():\n"
        "-    return None\n"
        "+    return value\n"
        "+    # anchored fix\n"
    )
    assert not pre_push_validation._diff_hunk_touches_line(diff_text, 200)
    assert pre_push_validation._diff_hunk_touches_line(diff_text, 42)
    assert pre_push_validation._diff_hunk_touches_line(diff_text, 43)


@pytest.mark.unit
def test_diff_hunk_touches_line_ignores_new_side_insert_false_positive() -> None:
    """PRRT_kwDOSJAM6s6bdI-h: new-side spans must not satisfy pre-fix anchors."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    unrelated_top_insert = "@@ -1,50 +1,200 @@\n"
    assert not pre_push_validation._diff_hunk_touches_line(unrelated_top_insert, 175)

    anchored_modification = "@@ -175,1 +200,1 @@\n"
    assert pre_push_validation._diff_hunk_touches_line(anchored_modification, 175)

    anchored_insertion = "@@ -175,0 +175,5 @@\n"
    assert pre_push_validation._diff_hunk_touches_line(anchored_insertion, 175)


@pytest.mark.unit
def test_diff_hunk_touches_line_detects_insert_before_review_anchor() -> None:
    """PRRT_kwDOSJAM6s6bdKiS: insert-before hunks use ``-(line-1),0 +line,N``."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    insert_before_anchor = "@@ -174,0 +175,5 @@\n"
    assert pre_push_validation._diff_hunk_touches_line(insert_before_anchor, 175)

    insert_before_first_line = "@@ -0,0 +1,3 @@\n"
    assert pre_push_validation._diff_hunk_touches_line(insert_before_first_line, 1)


@pytest.mark.unit
async def test_commit_range_touches_path_requires_review_line_overlap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """issue:5381831025: same-file deltas must overlap the inline review line."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "src").mkdir()
    target = repo / "src" / "target.py"
    target.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                "    return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    lines = target.read_text(encoding="utf-8").splitlines()
    lines[1] = "    return 2"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "unrelated line in same file")
    unrelated_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    lines = target.read_text(encoding="utf-8").splitlines()
    lines[4] = "    return value"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "anchored line")
    anchored_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=anchored_tip,
        path="src/target.py",
        line=5,
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=unrelated_tip,
        path="src/target.py",
        line=5,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_fails_closed_on_diff_errors(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stdout="", stderr="fatal: bad revision")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=worktree,
        left="1" * 40,
        right="2" * 40,
        path="src/target.py",
    )

    cmd2 = FakeCommandRunner()
    cmd2.queue_result(returncode=0, stdout="M\0src/target.py")  # missing terminating NUL
    runner2 = make_runner(
        factory=factory,
        cmd=cmd2,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner2,
        worktree_path=worktree,
        left="1" * 40,
        right="2" * 40,
        path="src/target.py",
    )


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

    # Mark the fixture repo before poisoning GIT_* so init/commit do not inherit
    # a broken object directory (CI: git commit SIGABRT under GIT_OBJECT_DIRECTORY).
    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)

    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    monkeypatch.setenv("GIT_GRAFT_FILE", "/tmp/agent-grafts")
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/replace")
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)

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
