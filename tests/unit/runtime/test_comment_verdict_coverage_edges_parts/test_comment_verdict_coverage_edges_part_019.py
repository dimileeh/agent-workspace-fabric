"""Comment verdict coverage edges: module store discovery, trusted HEAD probes, ignored-dir identity (split from part 011)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import AsyncioSubprocessRunner
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_with_dirty_submodule,
    init_git_worktree_with_embedded_repo,
)


@pytest.mark.unit
def test_module_git_dirs_under_and_nested_worktree_roots_helpers(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6e4egX: module walk + nested `.git` marker discovery."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_nested_helpers"
    worktree.mkdir()
    init_git_worktree_with_dirty_submodule(worktree)
    # This Git layout may keep ``sub/.git`` as a directory (no ``modules/``);
    # nested-marker discovery must still see the checkout.
    found_sub = fp_mod._nested_worktree_roots_with_git_markers(worktree)
    assert found_sub is not None
    assert any(path.name == "sub" for path in found_sub)

    # Synthetic ``modules/<name>`` tree under the outer git-dir.
    outer_git = (worktree / ".git").resolve()
    module_git = outer_git / "modules" / "synth"
    module_git.mkdir(parents=True)
    (module_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    modules = fp_mod._module_git_dirs_under(outer_git, roots=(worktree.resolve(),))
    assert modules is not None
    assert any(path.name == "synth" for path in modules)

    worktree2 = tmp_path / "ws_nested_helpers2"
    worktree2.mkdir()
    nested_name = init_git_worktree_with_embedded_repo(worktree2, nested_name="vendor_nested")
    found = fp_mod._nested_worktree_roots_with_git_markers(worktree2)
    assert found is not None
    assert any(path.name == nested_name for path in found)

    # Symlinked modules/ must fail closed.
    worktree3 = tmp_path / "ws_modules_symlink"
    worktree3.mkdir()
    init_git_worktree(worktree3)
    git_dir = (worktree3 / ".git").resolve()
    (git_dir / "modules").mkdir()
    (git_dir / "modules").rmdir()
    (git_dir / "modules").symlink_to(tmp_path / "elsewhere")
    assert fp_mod._module_git_dirs_under(git_dir, roots=(worktree3.resolve(),)) is None


@pytest.mark.unit
def test_module_git_dirs_under_traverses_slash_named_formal_stores(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fDnEJ: ``modules/libs/foo`` is a formal store, not ``libs/modules``."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_slash_module"
    worktree.mkdir()
    init_git_worktree(worktree)
    git_dir = (worktree / ".git").resolve()
    # Deinitialized submodule path ``libs/foo``: grouping dir + nested git-dir.
    module_git = git_dir / "modules" / "libs" / "foo"
    module_git.mkdir(parents=True)
    (module_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    # Nested formal store under the slash-named module.
    nested = module_git / "modules" / "inner"
    nested.mkdir(parents=True)
    (nested / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")

    modules = fp_mod._module_git_dirs_under(git_dir, roots=(worktree.resolve(),))
    assert modules is not None
    rel = {str(path.relative_to(git_dir / "modules")) for path in modules}
    assert "libs/foo" in rel
    assert "libs/foo/modules/inner" in rel
    assert "libs" not in rel

    # Symlinked ``config`` under a formal-store candidate must fail closed.
    worktree2 = tmp_path / "ws_slash_module_symlink_config"
    worktree2.mkdir()
    init_git_worktree(worktree2)
    git_dir2 = (worktree2 / ".git").resolve()
    poisoned = git_dir2 / "modules" / "vendor"
    poisoned.mkdir(parents=True)
    target = tmp_path / "elsewhere_config"
    target.write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (poisoned / "config").symlink_to(target)
    assert fp_mod._module_git_dirs_under(git_dir2, roots=(worktree2.resolve(),)) is None

    # Non-regular ``config`` (directory) must fail closed.
    worktree3 = tmp_path / "ws_slash_module_config_dir"
    worktree3.mkdir()
    init_git_worktree(worktree3)
    git_dir3 = (worktree3 / ".git").resolve()
    weird = git_dir3 / "modules" / "weird"
    weird.mkdir(parents=True)
    (weird / "config").mkdir()
    assert fp_mod._module_git_dirs_under(git_dir3, roots=(worktree3.resolve(),)) is None


@pytest.mark.unit
def test_module_git_dirs_under_discovers_slash_named_under_spoofed_grouping_config(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fECXY: spoofed grouping ``config`` must not hide ``libs/foo``."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_spoofed_grouping_config"
    worktree.mkdir()
    init_git_worktree(worktree)
    git_dir = (worktree / ".git").resolve()
    libs = git_dir / "modules" / "libs"
    libs.mkdir(parents=True)
    # Spoofed regular config makes ``libs`` look like a formal store.
    (libs / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    # Real slash-named submodule store beside the spoof.
    foo = libs / "foo"
    foo.mkdir()
    (foo / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    # Legitimate nested store under the spoofed formal candidate.
    nested = libs / "modules" / "inner"
    nested.mkdir(parents=True)
    (nested / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    # Git internals under the spoof must not be walked as grouping dirs.
    (libs / "objects" / "ab").mkdir(parents=True)

    modules = fp_mod._module_git_dirs_under(git_dir, roots=(worktree.resolve(),))
    assert modules is not None
    rel = {str(path.relative_to(git_dir / "modules")) for path in modules}
    assert "libs" in rel
    assert "libs/foo" in rel
    assert "libs/modules/inner" in rel
    assert not any(path.startswith("libs/objects") for path in rel)


@pytest.mark.unit
def test_module_git_dirs_under_discovers_slash_named_internal_basename_under_spoof(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fEPFh: skip list must not hide ``libs/objects`` formal stores."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_spoofed_internal_basename"
    worktree.mkdir()
    init_git_worktree(worktree)
    git_dir = (worktree / ".git").resolve()
    libs = git_dir / "modules" / "libs"
    libs.mkdir(parents=True)
    # Spoofed regular config makes ``libs`` look like a formal store.
    (libs / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    # Slash-named submodule path ``libs/objects`` — basename matches skip list.
    objects_store = libs / "objects"
    objects_store.mkdir()
    (objects_store / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    # Ordinary object shards under a different internal name must stay unlisted.
    (libs / "hooks" / "pre-commit.sample").parent.mkdir(parents=True)
    (libs / "hooks" / "pre-commit.sample").write_text("#!/bin/sh\n", encoding="utf-8")

    modules = fp_mod._module_git_dirs_under(git_dir, roots=(worktree.resolve(),))
    assert modules is not None
    rel = {str(path.relative_to(git_dir / "modules")) for path in modules}
    assert "libs" in rel
    assert "libs/objects" in rel
    assert "libs/hooks" not in rel


@pytest.mark.unit
def test_module_git_dirs_under_discovers_slash_named_through_internal_grouping(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fEmJn: internal-named grouping must not hide ``libs/hooks/foo``."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_spoofed_internal_grouping"
    worktree.mkdir()
    init_git_worktree(worktree)
    git_dir = (worktree / ".git").resolve()
    libs = git_dir / "modules" / "libs"
    libs.mkdir(parents=True)
    # Spoofed regular config makes ``libs`` look like a formal store.
    (libs / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    # Slash-named path ``libs/hooks/foo`` — ``hooks`` is a grouping dir, not a store.
    foo = libs / "hooks" / "foo"
    foo.mkdir(parents=True)
    (foo / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    # Loose-object shards under a planted ``objects/`` must not be listed as stores.
    (libs / "objects" / "ab").mkdir(parents=True)
    (libs / "objects" / "ab" / "cdef").write_text("blob", encoding="utf-8")
    (libs / "objects" / "pack").mkdir()
    (libs / "objects" / "pack" / "pack.idx").write_text("idx", encoding="utf-8")

    modules = fp_mod._module_git_dirs_under(git_dir, roots=(worktree.resolve(),))
    assert modules is not None
    rel = {str(path.relative_to(git_dir / "modules")) for path in modules}
    assert "libs" in rel
    assert "libs/hooks/foo" in rel
    assert "libs/hooks" not in rel
    assert not any(path.startswith("libs/objects") for path in rel)


@pytest.mark.unit
def test_module_git_dirs_under_discovers_slash_named_through_hex_grouping_under_objects(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fE1Te: hex grouping under objects must not hide ``libs/objects/ab/foo``."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_hex_objects_grouping"
    worktree.mkdir()
    init_git_worktree(worktree)
    git_dir = (worktree / ".git").resolve()
    libs = git_dir / "modules" / "libs"
    libs.mkdir(parents=True)
    # Spoofed regular config makes ``libs`` look like a formal store.
    (libs / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    # Slash-named path ``libs/objects/ab/foo`` — ``ab`` looks like a loose-object shard.
    foo = libs / "objects" / "ab" / "foo"
    foo.mkdir(parents=True)
    (foo / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    # Neighboring real-looking shard with only a loose-object file leaf must stay unlisted.
    (libs / "objects" / "cd").mkdir(parents=True)
    (libs / "objects" / "cd" / "cdef0123456789abcdef0123456789abcdef01").write_text(
        "blob",
        encoding="utf-8",
    )

    modules = fp_mod._module_git_dirs_under(git_dir, roots=(worktree.resolve(),))
    assert modules is not None
    rel = {str(path.relative_to(git_dir / "modules")) for path in modules}
    assert "libs" in rel
    assert "libs/objects/ab/foo" in rel
    assert "libs/objects/ab" not in rel
    assert "libs/objects/cd" not in rel
    assert not any(path == "libs/objects" for path in rel)

    # Intermediate grouping under a hex component must still reach the store.
    worktree2 = tmp_path / "ws_hex_objects_grouping_nested"
    worktree2.mkdir()
    init_git_worktree(worktree2)
    git_dir2 = (worktree2 / ".git").resolve()
    libs2 = git_dir2 / "modules" / "libs"
    libs2.mkdir(parents=True)
    (libs2 / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    bar = libs2 / "objects" / "ab" / "foo" / "bar"
    bar.mkdir(parents=True)
    (bar / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    modules2 = fp_mod._module_git_dirs_under(git_dir2, roots=(worktree2.resolve(),))
    assert modules2 is not None
    rel2 = {str(path.relative_to(git_dir2 / "modules")) for path in modules2}
    assert "libs/objects/ab/foo/bar" in rel2
    assert "libs/objects/ab/foo" not in rel2

    # Symlinked config under a hex-grouping child must fail closed.
    worktree3 = tmp_path / "ws_hex_objects_symlink_config"
    worktree3.mkdir()
    init_git_worktree(worktree3)
    git_dir3 = (worktree3 / ".git").resolve()
    libs3 = git_dir3 / "modules" / "libs"
    libs3.mkdir(parents=True)
    (libs3 / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    poisoned = libs3 / "objects" / "ab" / "foo"
    poisoned.mkdir(parents=True)
    target = tmp_path / "elsewhere_hex_config"
    target.write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (poisoned / "config").symlink_to(target)
    assert fp_mod._module_git_dirs_under(git_dir3, roots=(worktree3.resolve(),)) is None


@pytest.mark.unit
def test_subdirectory_children_under_objects_hex_shard_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fE1Te: hex-shard probe skips file leaves and fails closed."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested

    shard = tmp_path / "ab"
    shard.mkdir()
    (shard / "loose").write_text("blob", encoding="utf-8")
    grouping = shard / "foo"
    grouping.mkdir()
    roots = (tmp_path.resolve(),)

    with io_mod._residue_directory_enum_budget():
        found = nested._subdirectory_children_under_objects_hex_shard(
            shard,
            depth=0,
            roots=roots,
        )
    assert found is not None
    assert [path.name for path in found] == ["foo"]

    # Symlinked child fails closed.
    shard2 = tmp_path / "cd"
    shard2.mkdir()
    (shard2 / "link").symlink_to(tmp_path / "elsewhere")
    with io_mod._residue_directory_enum_budget():
        assert (
            nested._subdirectory_children_under_objects_hex_shard(
                shard2,
                depth=0,
                roots=roots,
            )
            is None
        )

    # Depth / deadline exhaustion fails closed.
    with io_mod._residue_directory_enum_budget():
        assert (
            nested._subdirectory_children_under_objects_hex_shard(
                shard,
                depth=io_mod._WORKTREE_DIRECTORY_ENUM_MAX_DEPTH + 1,
                roots=roots,
            )
            is None
        )

    # Unreadable shard fails closed.
    missing = tmp_path / "missing_shard"
    with io_mod._residue_directory_enum_budget():
        assert (
            nested._subdirectory_children_under_objects_hex_shard(
                missing,
                depth=0,
                roots=roots,
            )
            is None
        )

    # Entry-budget exhaustion while recording a subdirectory fails closed.
    shard3 = tmp_path / "ef"
    shard3.mkdir()
    (shard3 / "bar").mkdir()
    monkeypatch.setattr(io_mod, "_directory_enum_consume_entries", lambda _count: False)
    with io_mod._residue_directory_enum_budget():
        assert (
            nested._subdirectory_children_under_objects_hex_shard(
                shard3,
                depth=0,
                roots=roots,
            )
            is None
        )

    # Escape outside roots fails closed.
    shard4 = tmp_path / "a1"
    shard4.mkdir()
    (shard4 / "outside").mkdir()
    with io_mod._residue_directory_enum_budget():
        assert (
            nested._subdirectory_children_under_objects_hex_shard(
                shard4,
                depth=0,
                roots=(tmp_path / "other_root",),
            )
            is None
        )

    # Unreadable directory entry probe fails closed.
    shard5 = tmp_path / "a2"
    shard5.mkdir()
    (shard5 / "child").mkdir()
    monkeypatch.setattr(io_mod, "_directory_enum_consume_entries", lambda _count: True)

    class _BoomEntry:
        name = "child"

        def is_symlink(self) -> bool:
            return False

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            raise OSError(13, "permission denied")

        @property
        def path(self) -> str:
            return str(shard5 / "child")

    class _BoomScan:
        def __enter__(self) -> object:
            return iter([_BoomEntry()])

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(os, "scandir", lambda _path: _BoomScan())
    with io_mod._residue_directory_enum_budget():
        assert (
            nested._subdirectory_children_under_objects_hex_shard(
                shard5,
                depth=0,
                roots=roots,
            )
            is None
        )


@pytest.mark.unit
def test_is_loose_object_shard_name() -> None:
    """Loose-object shard helper accepts only two hex digits."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested

    assert nested._is_loose_object_shard_name("ab")
    assert nested._is_loose_object_shard_name("AB")
    assert nested._is_loose_object_shard_name("0f")
    assert not nested._is_loose_object_shard_name("abc")
    assert not nested._is_loose_object_shard_name("zz")
    assert not nested._is_loose_object_shard_name("pack")


@pytest.mark.unit
def test_formal_module_store_is_git_dir_oserror_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fDnEJ: unreadable ``config`` probe fails closed."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_nested as nested

    probe = tmp_path / "store"
    probe.mkdir()
    (probe / "config").write_text("[core]\n", encoding="utf-8")
    real_lstat = Path.lstat

    def _boom(self: Path) -> os.stat_result:
        if self.name == "config":
            raise OSError(13, "permission denied")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _boom)
    assert nested._formal_module_store_is_git_dir(probe) is None


@pytest.mark.unit
@pytest.mark.timeout(5)
def test_module_git_dirs_under_fails_closed_when_enum_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e5zYG: wide modules/ trees must honor directory-enum budget."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io

    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_DIRECTORY_ENUM_AGGREGATE_MAX_ENTRIES",
        8,
    )
    worktree = tmp_path / "ws_modules_budget"
    worktree.mkdir()
    init_git_worktree(worktree)
    git_dir = (worktree / ".git").resolve()
    modules = git_dir / "modules"
    modules.mkdir()
    for index in range(20):
        child = modules / f"mod_{index:02d}"
        child.mkdir()
        (child / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")

    assert fp_mod._module_git_dirs_under(git_dir, roots=(worktree.resolve(),)) is None


@pytest.mark.unit
@pytest.mark.timeout(5)
def test_module_git_dirs_under_fails_closed_when_depth_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e5zYG: deeply nested modules/ must honor max-depth budget."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io

    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_WORKTREE_DIRECTORY_ENUM_MAX_DEPTH",
        2,
    )
    worktree = tmp_path / "ws_modules_depth"
    worktree.mkdir()
    init_git_worktree(worktree)
    git_dir = (worktree / ".git").resolve()
    cursor = git_dir / "modules"
    for index in range(5):
        cursor.mkdir(parents=True, exist_ok=True)
        nested = cursor / f"deep_{index}"
        nested.mkdir()
        (nested / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
        cursor = nested / "modules"

    assert fp_mod._module_git_dirs_under(git_dir, roots=(worktree.resolve(),)) is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_protocol_retry_rollback_initial_head_avoids_fifo_via_trusted_reader(
    tmp_path: Path,
) -> None:
    """Review 5101264783: rollback pre-restore HEAD must use remembered configs.

    Attempt 0 can inject ``include.path`` → FIFO before non-FIXED rollback. The
    initial HEAD probe runs before Git configuration restore; a live
    ``_rev_parse_head`` would hang. Route through the trusted reader instead.
    """
    from awf.runtime.pr_monitor_runner import (
        comment_verdict,
        comment_verdict_residue_fingerprint,
    )

    worktree = tmp_path / "ws_rollback_fifo_head"
    worktree.mkdir()
    init_git_worktree(worktree)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert comment_verdict_residue_fingerprint.remember_item_start_local_git_configs(worktree)

    fifo = tmp_path / "rollback_poison.fifo"
    os.mkfifo(fifo, mode=0o644)
    subprocess.run(
        ["git", "config", "include.path", str(fifo)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    (worktree / "agent-edit.txt").write_text("scratch\n", encoding="utf-8")

    async def _live_rev_parse(_path: Path, **_kwargs: object) -> str | None:
        raise AssertionError("covered snapshot must not fall back to live rev-parse")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=AsyncioSubprocessRunner(),
        ),
        _rev_parse_head=_live_rev_parse,
    )
    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_rollback_fifo_head",
        worktree_path=worktree,
        item_start_head=head,
        state=None,
    )
    assert not (worktree / "agent-edit.txt").exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protocol_retry_rollback_initial_head_fallback_passes_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 5101264783: no-snapshot rollback HEAD fallback must be finite."""
    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_residue
    from awf.runtime.validation_worktree import (
        ValidationWorktreeCheck,
        ValidationWorktreeCleanup,
    )

    worktree = tmp_path / "ws_rollback_timeout_fallback"
    worktree.mkdir()
    start = "a" * 40
    captured: dict[str, object] = {}

    async def _cleanup(**_kwargs: object) -> ValidationWorktreeCleanup:
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True, paths=()),
            restore_ref=start,
        )

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )

    async def _rev_parse_head(_path: Path, *, timeout_seconds: float | None = None) -> str:
        captured["timeout_seconds"] = timeout_seconds
        return start

    async def _run(cmd: list[str], **kwargs: object) -> CommandResult:
        del cmd
        captured.setdefault("run_timeouts", []).append(kwargs.get("timeout_seconds"))
        return CommandResult(returncode=0, stdout=f"{start}\n", stderr="")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=SimpleNamespace(run=_run),
        ),
        _rev_parse_head=_rev_parse_head,
    )
    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_rollback_timeout_fallback",
        worktree_path=worktree,
        item_start_head=start,
        state=None,
    )
    assert (
        captured["timeout_seconds"] == comment_verdict_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS
    )
    assert comment_verdict_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS in (
        captured.get("run_timeouts") or []
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protocol_retry_rollback_live_head_recheck_passes_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fG5gp: post-restore live HEAD recheck must be bounded."""
    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_residue
    from awf.runtime.validation_worktree import (
        ValidationWorktreeCheck,
        ValidationWorktreeCleanup,
    )

    worktree = tmp_path / "ws_rollback_live_head_timeout"
    worktree.mkdir()
    start = "a" * 40
    run_timeouts: list[float | None] = []

    async def _cleanup(**_kwargs: object) -> ValidationWorktreeCleanup:
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True, paths=()),
            restore_ref=start,
        )

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )

    async def _rev_parse_head(_path: Path, *, timeout_seconds: float | None = None) -> str:
        del timeout_seconds
        return start

    async def _run(cmd: list[str], **kwargs: object) -> CommandResult:
        del cmd
        timeout_value = kwargs.get("timeout_seconds")
        run_timeouts.append(timeout_value if isinstance(timeout_value, (int, float)) else None)
        return CommandResult(returncode=0, stdout=f"{start}\n", stderr="")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=SimpleNamespace(run=_run),
        ),
        _rev_parse_head=_rev_parse_head,
    )
    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_rollback_live_head_timeout",
        worktree_path=worktree,
        item_start_head=start,
        state=None,
    )
    # The repinned HEAD read under the writer lock (and any reset that follows)
    # must carry the residue ordinary timeout (no unbounded live Git).
    assert len(run_timeouts) >= 1
    assert all(
        timeout == comment_verdict_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS
        for timeout in run_timeouts
    )


@pytest.mark.unit
def test_ignored_dir_hash_falls_back_to_metadata_when_content_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e4fPN: oversized ignored dirs must not yield None identity.

    Content hashing reuses the 32 MiB worktree budget; typical ignored roots
    exceed it. Failing closed treats a stable large tree as mutation and rejects
    clean non-FIXED corrections. Metadata identity must still differ on size change.
    """
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_ignored_budget"
    worktree.mkdir()
    init_git_worktree(worktree)
    vendor = worktree / "vendor"
    vendor.mkdir()
    (vendor / "a").write_text("one\n", encoding="utf-8")
    (vendor / "b").write_text("two\n", encoding="utf-8")

    monkeypatch.setattr(
        residue,
        "_hash_worktree_directory_residue",
        lambda **_kwargs: None,
    )
    git_env = git_env_without_object_lookup_overrides()
    baseline = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert baseline is not None
    repeat = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert repeat == baseline
    (vendor / "a").write_text("one-mutated-longer\n", encoding="utf-8")
    mutated = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert mutated is not None and mutated != baseline


@pytest.mark.unit
@pytest.mark.timeout(60)
def test_ignored_dir_metadata_fallback_stable_with_oversized_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e7oIu: one oversized blob must not collapse overflow identity."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_oversized_blob"
    worktree.mkdir()
    init_git_worktree(worktree)
    vendor = worktree / "vendor"
    vendor.mkdir()
    sample = io_mod._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    oversize = io_mod._WORKTREE_REGULAR_HASH_MAX_FILE_BYTES + sample
    (vendor / "large.bin").write_bytes(b"L" * oversize)
    (vendor / "small.txt").write_text("pad\n", encoding="utf-8")

    monkeypatch.setattr(
        residue,
        "_hash_worktree_directory_residue",
        lambda **_kwargs: None,
    )
    git_env = git_env_without_object_lookup_overrides()
    baseline = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert baseline is not None
    repeat = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert repeat == baseline


@pytest.mark.unit
def test_ignored_dir_metadata_fallback_detects_same_size_content_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e5nwj: overflow fallback must keep content-derived identity.

    When content hashing fails closed on budget, a same-size overwrite that
    restores ``mtime_ns`` must still change the ignored-dir fingerprint so
    rollback does not accept altered dependency/config bytes.
    """
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_ignored_same_size"
    worktree.mkdir()
    init_git_worktree(worktree)
    vendor = worktree / "vendor"
    vendor.mkdir()
    target = vendor / "pkg.json"
    target.write_text('{"v":1}\n', encoding="utf-8")
    (vendor / "other.txt").write_text("pad\n", encoding="utf-8")

    monkeypatch.setattr(
        residue,
        "_hash_worktree_directory_residue",
        lambda **_kwargs: None,
    )
    git_env = git_env_without_object_lookup_overrides()
    baseline = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert baseline is not None

    st = target.stat()
    # Same byte length as '{"v":1}\n' so size+mtime metadata would collide.
    target.write_text('{"v":2}\n', encoding="utf-8")
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    mutated = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert mutated is not None and mutated != baseline
