"""Pre-push validation fix-pass rename and tree comparison edge cases (part 10)."""

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
    # Unrelated region sits well below the review line so near-anchor proximity
    # and call-site→definition linking cannot accept it as FIXED evidence.
    target.write_text(
        "\n".join(
            [
                "def reviewed():",
                "    return None",
                "",
                *[f"# spacer {i}" for i in range(20)],
                "def far_helper():",
                "    return 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    lines = target.read_text(encoding="utf-8").splitlines()
    far_idx = lines.index("    return 1")
    lines[far_idx] = "    return 2"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "unrelated line in same file")
    unrelated_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "reset", "--hard", start)
    lines = target.read_text(encoding="utf-8").splitlines()
    lines[1] = "    return value"
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
        line=2,
    )
    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=unrelated_tip,
        path="src/target.py",
        line=2,
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
