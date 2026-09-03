"""Trusted outer HEAD probe: reject symlinked object/ref stores (part 15)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import AsyncioSubprocessRunner
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
)


def _init_foreign_repo(tmp_path: Path, *, name: str) -> tuple[Path, str]:
    foreign = tmp_path / name
    foreign.mkdir()
    init_git_worktree(foreign)
    (foreign / "evil.txt").write_text("evil\n", encoding="utf-8")
    subprocess.run(["git", "add", "evil.txt"], cwd=foreign, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "evil"], cwd=foreign, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=foreign,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return foreign, head


def _default_branch_loose_ref(git_dir: Path) -> Path:
    heads = git_dir / "refs" / "heads"
    for name in ("main", "master"):
        candidate = heads / name
        if candidate.exists():
            return candidate
    raise AssertionError(f"no default branch ref under {heads}")


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_trusted_head_probe_rejects_symlinked_refs_store(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fFF47: refs symlink must not chain foreign tips into HEAD."""

    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_trusted_refs_symlink"
    worktree.mkdir()
    init_git_worktree(worktree)
    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert fp_mod.remember_item_start_local_git_configs(worktree) is True

    foreign, foreign_head = _init_foreign_repo(tmp_path, name="foreign_refs")
    assert foreign_head != local_head

    refs = worktree / ".git" / "refs"
    shutil.rmtree(refs)
    refs.symlink_to(foreign / ".git" / "refs")

    with fp_mod.item_start_trusted_head_probe_git_dir(worktree) as probe:
        assert probe is None

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    parsed = await fp_mod.read_protocol_attempt_start_head(
        runner,
        worktree_path=worktree,
        rev_parse_head=None,
    )
    assert parsed is None
    assert parsed != foreign_head


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_trusted_head_probe_rejects_symlinked_packed_refs(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fFF47: packed-refs symlink must not supply a foreign tip."""

    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_trusted_packed_refs_symlink"
    worktree.mkdir()
    init_git_worktree(worktree)
    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert fp_mod.remember_item_start_local_git_configs(worktree) is True

    foreign, foreign_head = _init_foreign_repo(tmp_path, name="foreign_packed")
    assert foreign_head != local_head
    subprocess.run(["git", "pack-refs", "--all"], cwd=foreign, check=True, capture_output=True)
    foreign_packed = foreign / ".git" / "packed-refs"
    assert foreign_packed.is_file()
    assert foreign_head in foreign_packed.read_text(encoding="utf-8")

    packed = worktree / ".git" / "packed-refs"
    if packed.exists() or packed.is_symlink():
        packed.unlink()
    packed.symlink_to(foreign_packed)
    _default_branch_loose_ref(worktree / ".git").unlink()

    with fp_mod.item_start_trusted_head_probe_git_dir(worktree) as probe:
        assert probe is None

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    parsed = await fp_mod.read_protocol_attempt_start_head(
        runner,
        worktree_path=worktree,
        rev_parse_head=None,
    )
    assert parsed is None
    assert parsed != foreign_head


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_trusted_head_probe_rejects_symlinked_objects_store(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fFF47: objects symlink must not chain foreign stores."""

    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_trusted_objects_symlink"
    worktree.mkdir()
    init_git_worktree(worktree)
    assert fp_mod.remember_item_start_local_git_configs(worktree) is True

    foreign, _foreign_head = _init_foreign_repo(tmp_path, name="foreign_objects")
    objects = worktree / ".git" / "objects"
    shutil.rmtree(objects)
    objects.symlink_to(foreign / ".git" / "objects")

    with fp_mod.item_start_trusted_head_probe_git_dir(worktree) as probe:
        assert probe is None
