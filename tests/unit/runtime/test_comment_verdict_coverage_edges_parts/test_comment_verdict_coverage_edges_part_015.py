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
    # Configless marker: no config/config.worktree, but HEAD identity is folded in
    # for tip-only mutation detection (PRRT_kwDOSJAM6s6fG5gn).
    assert "config" not in snap[nested_key]
    assert "config.worktree" not in snap[nested_key]
    assert snap[nested_key].get("HEAD") == "ref: refs/heads/main\n"

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


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_correction_residue_fingerprint_surfaces_preexisting_nested_head_tip_change(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fG5gn: nested HEAD tip under tracked path must change git-meta.

    A pre-existing ``src/.git`` keeps the same marker key and config text while a
    correction advances the symbolic-ref tip. Outer porcelain stays clean; the
    metadata fingerprint must still change so non-FIXED cannot be accepted.
    """
    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_preexisting_nested_head"
    worktree.mkdir()
    init_git_worktree(worktree)

    nested_git = worktree / "src" / ".git"
    (nested_git / "objects").mkdir(parents=True)
    (nested_git / "refs" / "heads").mkdir(parents=True)
    (nested_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (nested_git / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n",
        encoding="utf-8",
    )
    (nested_git / "refs" / "heads" / "main").write_text(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        encoding="utf-8",
    )

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="", stderr="", stdout_bytes=b"")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_preexisting_nested_head",
        worktree_path=worktree,
    )
    assert start_fp is not None
    assert start_fp.startswith("git-meta:")

    start_snap = fp_mod._snapshot_worktree_local_git_configs(worktree)
    assert start_snap is not None
    nested_key = str(nested_git.resolve())
    assert nested_key in start_snap
    assert "HEAD" in start_snap[nested_key]
    assert start_snap[nested_key]["config"] == "[core]\n\trepositoryformatversion = 0\n"

    (nested_git / "refs" / "heads" / "main").write_text(
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n",
        encoding="utf-8",
    )

    plain_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert plain_status.stdout.strip() == ""

    after_snap = fp_mod._snapshot_worktree_local_git_configs(worktree)
    assert after_snap is not None
    assert after_snap[nested_key]["HEAD"] == start_snap[nested_key]["HEAD"]
    assert after_snap[nested_key]["config"] == start_snap[nested_key]["config"]
    assert after_snap[nested_key]["HEAD.tip"] != start_snap[nested_key]["HEAD.tip"]

    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_preexisting_nested_head",
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


@pytest.mark.unit
def test_hash_local_git_config_snapshot_includes_nested_head_identity() -> None:
    """PRRT_kwDOSJAM6s6fG5gn: HEAD / HEAD.tip keys must affect the git-meta digest."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    base = {
        "/ws/src/.git": {
            "config": "[core]\n\trepositoryformatversion = 0\n",
            "HEAD": "ref: refs/heads/main\n",
            "HEAD.tip": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        }
    }
    tip_changed = {
        "/ws/src/.git": {
            **base["/ws/src/.git"],
            "HEAD.tip": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n",
        }
    }
    head_changed = {
        "/ws/src/.git": {
            **base["/ws/src/.git"],
            "HEAD": "cccccccccccccccccccccccccccccccccccccccc\n",
            "HEAD.tip": "cccccccccccccccccccccccccccccccccccccccc\n",
        }
    }
    assert fp_mod._hash_local_git_config_snapshot(base) != fp_mod._hash_local_git_config_snapshot(
        tip_changed
    )
    assert fp_mod._hash_local_git_config_snapshot(base) != fp_mod._hash_local_git_config_snapshot(
        head_changed
    )


@pytest.mark.unit
def test_snapshot_git_dir_head_identity_packed_refs_and_symlink_fail_closed(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fG5gn: packed-refs tip + refuse symlinked HEAD/ref."""
    from awf.runtime.pr_monitor_runner import (
        comment_verdict_residue_fingerprint_git_config as git_cfg,
    )

    git_dir = tmp_path / "nested.git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "dddddddddddddddddddddddddddddddddddddddd refs/heads/main\n",
        encoding="utf-8",
    )
    packed = git_cfg._snapshot_git_dir_head_identity_fields(git_dir)
    assert packed == {
        "HEAD": "ref: refs/heads/main\n",
        "HEAD.tip": "dddddddddddddddddddddddddddddddddddddddd\n",
    }

    absent = tmp_path / "absent.git"
    absent.mkdir()
    assert git_cfg._snapshot_git_dir_head_identity_fields(absent) == {}

    link_git = tmp_path / "link.git"
    link_git.mkdir()
    target = tmp_path / "evil_HEAD"
    target.write_text("ref: refs/heads/main\n", encoding="utf-8")
    (link_git / "HEAD").symlink_to(target)
    assert git_cfg._snapshot_git_dir_head_identity_fields(link_git) is None


@pytest.mark.unit
def test_snapshot_git_dir_head_identity_reads_oversized_packed_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fHJIm: packed-refs tip must not use the config-file cap."""
    from awf.node import git_manager_ownership
    from awf.runtime.pr_monitor_runner import (
        comment_verdict_residue_fingerprint_git_config as git_cfg,
    )

    # Config max stays tiny; packed-refs must still stream past it.
    monkeypatch.setattr(git_manager_ownership, "_GIT_DIR_CONFIG_MAX_BYTES", 64)

    tip = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    padding = "".join(f"{i:040x} refs/heads/pad-{i:05d}\n" for i in range(200))
    packed_body = f"# pack-refs with: peeled fully-peeled sorted\n{padding}{tip} refs/heads/main\n"
    assert len(packed_body.encode("utf-8")) > 64

    git_dir = tmp_path / "mirror.git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "packed-refs").write_text(packed_body, encoding="utf-8")

    # Sanity: the config reader still refuses this file under the tiny cap.
    assert git_manager_ownership._read_git_dir_config_text(git_dir / "packed-refs") is None

    fields = git_cfg._snapshot_git_dir_head_identity_fields(git_dir)
    assert fields == {
        "HEAD": "ref: refs/heads/main\n",
        "HEAD.tip": f"{tip}\n",
    }

    # Missing packed-refs → empty tip (unborn / not packed).
    bare = tmp_path / "unborn.git"
    (bare / "refs" / "heads").mkdir(parents=True)
    (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert git_cfg._snapshot_git_dir_head_identity_fields(bare) == {
        "HEAD": "ref: refs/heads/main\n",
        "HEAD.tip": "",
    }

    # Symlinked packed-refs must fail closed (O_NOFOLLOW).
    link_pack = tmp_path / "link_pack.git"
    (link_pack / "refs" / "heads").mkdir(parents=True)
    (link_pack / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    target_pack = tmp_path / "evil_packed_refs"
    target_pack.write_text(
        f"{tip} refs/heads/main\n",
        encoding="utf-8",
    )
    (link_pack / "packed-refs").symlink_to(target_pack)
    assert git_cfg._snapshot_git_dir_head_identity_fields(link_pack) is None


@pytest.mark.unit
def test_read_packed_refs_tip_rejects_scan_budget_and_absent_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fHJIm: dedicated scan cap + absent-ref empty tip."""
    from awf.runtime.pr_monitor_runner import (
        comment_verdict_residue_fingerprint_git_config as git_cfg,
    )

    packed = tmp_path / "packed-refs"
    packed.write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb refs/heads/other\n",
        encoding="utf-8",
    )
    assert git_cfg._read_packed_refs_tip_for_name(packed, "refs/heads/main") == ""

    monkeypatch.setattr(git_cfg, "_PACKED_REFS_SCAN_MAX_BYTES", 8)
    huge = tmp_path / "huge-packed-refs"
    huge.write_text("x" * 64 + "\n", encoding="utf-8")
    assert git_cfg._read_packed_refs_tip_for_name(huge, "refs/heads/main") is None
    monkeypatch.setattr(git_cfg, "_PACKED_REFS_SCAN_MAX_BYTES", 64 * 1024 * 1024)

    # Final line without trailing newline still resolves.
    no_nl = tmp_path / "no-nl-packed-refs"
    no_nl.write_text(
        "cccccccccccccccccccccccccccccccccccccccc refs/heads/main",
        encoding="utf-8",
    )
    assert (
        git_cfg._read_packed_refs_tip_for_name(no_nl, "refs/heads/main")
        == "cccccccccccccccccccccccccccccccccccccccc\n"
    )

    monkeypatch.setattr(git_cfg, "_PACKED_REFS_SCAN_MAX_LINE_BYTES", 16)
    monkeypatch.setattr(git_cfg, "_PACKED_REFS_SCAN_CHUNK_BYTES", 8)
    long_line = tmp_path / "long-line-packed-refs"
    long_line.write_text("a" * 64, encoding="utf-8")
    assert git_cfg._read_packed_refs_tip_for_name(long_line, "refs/heads/main") is None
    monkeypatch.setattr(git_cfg, "_PACKED_REFS_SCAN_CHUNK_BYTES", 64 * 1024)

    monkeypatch.setattr(git_cfg, "_PACKED_REFS_SCAN_MAX_LINE_BYTES", 64 * 1024)
    monkeypatch.setattr(git_cfg, "_PACKED_REFS_SCAN_BUDGET_SECONDS", 0.0)
    assert git_cfg._read_packed_refs_tip_for_name(packed, "refs/heads/main") is None


@pytest.mark.unit
def test_read_packed_refs_tip_honors_config_snapshot_deadline(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fHliF: packed-refs scan must honor snapshot wall budget."""
    import time

    from awf.node import git_manager_ownership
    from awf.runtime.pr_monitor_runner import (
        comment_verdict_residue_fingerprint_git_config as git_cfg,
    )

    packed = tmp_path / "packed-refs"
    packed.write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "dddddddddddddddddddddddddddddddddddddddd refs/heads/main\n",
        encoding="utf-8",
    )
    # Outside a budget context the tip still resolves (standalone probe).
    assert (
        git_cfg._read_packed_refs_tip_for_name(packed, "refs/heads/main")
        == "dddddddddddddddddddddddddddddddddddddddd\n"
    )

    budget = git_manager_ownership._GitConfigSnapshotBudget(
        bytes_remaining=git_manager_ownership._GIT_CONFIG_SNAPSHOT_AGGREGATE_MAX_BYTES,
        deadline=time.monotonic() - 1.0,
    )
    token = git_manager_ownership._GIT_CONFIG_SNAPSHOT_BUDGET.set(budget)
    try:
        assert git_cfg._read_packed_refs_tip_for_name(packed, "refs/heads/main") is None
    finally:
        git_manager_ownership._GIT_CONFIG_SNAPSHOT_BUDGET.reset(token)
