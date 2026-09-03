"""Trusted outer HEAD probe + configless nested git-meta hashing (part 15)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import AsyncioSubprocessRunner
from awf.runtime.pr_monitor_runner import comment_verdict_residue
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


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_trusted_head_probe_skips_oversized_object_store_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fGb8b: HEAD probe must not copy live packs under leaf caps."""

    from awf.node import git_manager_ownership as ownership
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_trusted_head_large_pack"
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

    # A pack larger than the nested leaf-copy max would fail closed if the
    # trusted HEAD probe still materializes the live object store.
    pack_dir = worktree / ".git" / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    oversized = pack_dir / "pack-oversized.pack"
    oversized.write_bytes(b"P" * 2048)
    monkeypatch.setattr(ownership, "_OBJECT_STORE_LEAF_COPY_MAX_BYTES", 512)

    def _fail_if_objects_copied(*_args: object, **_kwargs: object) -> tuple[bool, list[int]]:
        raise AssertionError("trusted HEAD probe must not copy the live object store")

    monkeypatch.setattr(
        ownership,
        "_symlink_nested_probe_objects_store_via_fd",
        _fail_if_objects_copied,
    )

    with fp_mod.item_start_trusted_head_probe_git_dir(worktree) as probe:
        assert probe is not None
        assert (probe / "objects").is_dir()
        assert not any((probe / "objects").rglob("*"))
        resolved = subprocess.run(
            ["git", "--git-dir", str(probe), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert resolved.lower() == local_head.lower()

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    parsed = await fp_mod.read_protocol_attempt_start_head(
        runner,
        worktree_path=worktree,
        rev_parse_head=None,
    )
    assert parsed is not None
    assert parsed.lower() == local_head.lower()


@pytest.mark.unit
def test_hash_local_git_config_snapshot_includes_configless_git_dirs() -> None:
    """PRRT_kwDOSJAM6s6fGqDa: empty config maps must still hash the git-dir key."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    absent = fp_mod._hash_local_git_config_snapshot({})
    empty_map = fp_mod._hash_local_git_config_snapshot({"/ws/src/.git": {}})
    with_config = fp_mod._hash_local_git_config_snapshot(
        {"/ws/src/.git": {"config": "[core]\n\trepositoryformatversion = 0\n"}}
    )
    assert empty_map != absent
    assert empty_map != with_config
    assert with_config != absent


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_correction_residue_fingerprint_surfaces_configless_nested_git_dir(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fGqDa: configless nested ``.git`` must change git-meta.

    A correction can plant ``src/.git/{HEAD,objects/,refs/}`` with no config
    files. Outer porcelain stays clean and the snapshot records ``{}``, but the
    metadata fingerprint must still change so non-FIXED cannot be accepted.
    """
    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_configless_nested_git"
    worktree.mkdir()
    init_git_worktree(worktree)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="", stderr="", stdout_bytes=b"")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_configless_nested_git",
        worktree_path=worktree,
    )
    assert start_fp is not None
    assert start_fp.startswith("git-meta:")

    nested_git = worktree / "src" / ".git"
    (nested_git / "objects").mkdir(parents=True)
    (nested_git / "refs").mkdir(parents=True)
    (nested_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert not (nested_git / "config").exists()

    plain_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert plain_status.stdout.strip() == ""

    snap = fp_mod._snapshot_worktree_local_git_configs(worktree)
    assert snap is not None
    nested_key = str(nested_git.resolve())
    assert nested_key in snap
    assert snap[nested_key] == {}

    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_configless_nested_git",
        worktree_path=worktree,
    )
    assert poisoned_fp is not None
    assert poisoned_fp.startswith("git-meta:")
    assert poisoned_fp != start_fp
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=poisoned_fp,
    )
