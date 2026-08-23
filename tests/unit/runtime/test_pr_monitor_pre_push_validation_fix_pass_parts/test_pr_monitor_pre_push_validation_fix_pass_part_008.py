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
    assert pre_push_validation._map_review_line_through_diff(10, top_insert) == 15

    insert_before_anchor = "@@ -174,0 +175,5 @@\n"
    # PRRT_kwDOSJAM6s6bdSlA: insert-before keeps old_start unmoved.
    assert pre_push_validation._map_review_line_through_diff(174, insert_before_anchor) == 174
    assert pre_push_validation._map_review_line_through_diff(175, insert_before_anchor) == 180

    unchanged_tail = "@@ -5,1 +5,1 @@\n"
    assert pre_push_validation._map_review_line_through_diff(10, unchanged_tail) == 10


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
