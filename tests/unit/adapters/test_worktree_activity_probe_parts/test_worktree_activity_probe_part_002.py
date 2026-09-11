"""Worktree activity probe: walk uncertainty and priming (continued from part 001).

Uncertainty fails open: a walk that could not observe the whole tree — the entry
budget ran out, a directory was unreadable, or an entry could not be stat-ed —
answers ``None`` ("could not tell"), which the watchdog counts as activity,
never as idleness.

Split out of ``tests/unit/adapters/test_worktree_activity_probe.py`` to keep
each test module under the first-party 1500-line maintainability guardrail.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest
import structlog

from awf.adapters import worktree_activity
from awf.adapters.worktree_activity import (
    WorktreeActivityProbe,
    make_worktree_activity_probe,
)
from tests.unit.adapters.test_worktree_activity_probe_parts.helpers import (
    _age,
    _age_tree,
    _await_scan_gate,
    _prime_scan_truncates,
)


@pytest.mark.unit
async def test_git_directory_is_walked_like_any_other_path(worktree: Path) -> None:
    """A real ``.git`` directory is not excluded — the agent may write there."""
    git_dir = worktree / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    _age_tree(worktree)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_symlinked_git_directory_is_watched_like_a_linked_worktree(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A ``.git`` *symlink* to a git dir keeps Git state outside the walk.

    The walk lstats every entry and never descends through a symlink, so a
    symlinked ``.git`` is as external to it as a linked worktree's pointer
    target. Treating it as a plain checkout dropped HEAD / index / logs/HEAD
    from an otherwise complete-looking fingerprint, so a print-mode agent that
    was still committing looked idle.
    """
    git_dir = tmp_path / "real.git"
    (git_dir / "logs").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "index").write_bytes(b"DIRC")
    (git_dir / "logs" / "HEAD").write_text("reflog\n", encoding="utf-8")
    (worktree / ".git").symlink_to(git_dir, target_is_directory=True)
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "index").write_bytes(b"DIRC-updated")
    assert await probe() is True


@pytest.mark.unit
async def test_symlinked_git_directory_is_walked_whole(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A symlinked git dir is walked, not just stat-ed at the paths named above.

    ``_git_common_dir`` answers the git dir itself for a plain repo, so keying
    the walk root off "is this a linked worktree?" left a symlinked ``.git``
    covered only by ``HEAD`` / ``index`` / ``logs/HEAD`` / the branch ref.
    Metadata-only work — ``git tag``, a second branch, a ``packed-refs``
    rewrite — lands under ``refs/`` and moves none of those, so the fingerprint
    held still and a silent print-mode agent was idle-killed mid-run. A plain
    ``.git`` *directory* is walked whole by the worktree walk; going through a
    symlink must not quietly narrow that to a handful of names.
    """
    git_dir = tmp_path / "real.git"
    (git_dir / "refs" / "tags").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (worktree / ".git").symlink_to(git_dir, target_is_directory=True)
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "refs" / "tags" / "v1").write_text("deadbeef\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_shared_common_dir_is_still_not_walked(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A linked worktree walks its own git dir and never the shared common one.

    One bare mirror backs every worktree of a repo, so churn under the common
    dir is other workspaces' agents — counting it would report this worktree as
    alive whatever it is doing, and its object store would burn the walk budget.
    """
    common_dir = tmp_path / "mirror.git"
    git_dir = common_dir / "worktrees" / "ws_probe"
    (git_dir / "rebase-merge").mkdir(parents=True)
    (common_dir / "objects").mkdir(parents=True)
    (git_dir / "commondir").write_text(f"{common_dir}\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(common_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    # Another workspace's agent churning the shared mirror is not this
    # worktree's activity.
    (common_dir / "objects" / "pack-other").write_text("x\n", encoding="utf-8")
    assert await probe() is False

    # This worktree's own git dir still is.
    (git_dir / "rebase-merge" / "done").write_text("pick\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_symlinked_gitfile_pointer_still_resolves(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A ``.git`` symlink to a ``gitdir:`` *file* resolves through to the git dir."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "index").write_bytes(b"DIRC")
    gitfile = tmp_path / "gitfile"
    gitfile.write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    (worktree / ".git").symlink_to(gitfile)
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "index").write_bytes(b"DIRC-updated")
    assert await probe() is True


@pytest.mark.unit
@pytest.mark.parametrize("replacement", ["pointer", "symlink"])
async def test_primed_probe_never_walks_replaced_git_target(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    """Agent-controlled ``.git`` replacement cannot redirect a control-plane walk."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "index").write_bytes(b"DIRC")
    gitfile = worktree / ".git"
    gitfile.write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    untrusted = tmp_path / "agent-selected"
    untrusted.mkdir()
    if replacement == "pointer":
        gitfile.write_text(f"gitdir: {untrusted}\n", encoding="utf-8")
    else:
        gitfile.unlink()
        gitfile.symlink_to(untrusted, target_is_directory=True)

    real_scandir = os.scandir

    def _reject_untrusted_walk(path: str | int) -> os.ScandirIterator[str]:
        if not isinstance(path, int):
            candidate = Path(path)
            if candidate == untrusted or untrusted in candidate.parents:
                raise AssertionError(f"walk escaped to agent-selected path: {candidate}")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _reject_untrusted_walk)

    assert await probe() is True
    (git_dir / "index").write_bytes(b"DIRC-updated")
    assert await probe() is True


@pytest.mark.unit
@pytest.mark.parametrize("replacement", ["pointer", "symlink"])
async def test_new_probe_rejects_git_target_rewritten_by_an_earlier_invocation(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    """GitManager roots, not a prior agent's marker, anchor each new probe."""
    common_dir = tmp_path / "mirror.git"
    git_dir = common_dir / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    gitfile = worktree / ".git"
    gitfile.write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    trusted_git_roots = (git_dir, common_dir)

    first = await make_worktree_activity_probe(
        worktree,
        trusted_git_roots=trusted_git_roots,
    )
    assert first is not None
    assert await first() is False

    untrusted = tmp_path / "agent-selected-between-invocations"
    untrusted.mkdir()
    (untrusted / "sentinel").write_text("outside\n", encoding="utf-8")
    if replacement == "pointer":
        gitfile.write_text(f"gitdir: {untrusted}\n", encoding="utf-8")
    else:
        gitfile.unlink()
        gitfile.symlink_to(untrusted, target_is_directory=True)

    real_pin_directory = worktree_activity._pin_directory

    def _reject_untrusted_pin(path: Path) -> object:
        if path == untrusted:
            raise AssertionError(f"probe trusted agent-selected Git root: {path}")
        return real_pin_directory(path)

    monkeypatch.setattr(worktree_activity, "_pin_directory", _reject_untrusted_pin)

    second = await make_worktree_activity_probe(
        worktree,
        trusted_git_roots=trusted_git_roots,
    )
    assert second is not None
    assert await second() is None


@pytest.mark.unit
async def test_new_probe_rejects_commondir_rewritten_by_an_earlier_invocation(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """GitManager's common dir must still be the one Git actually uses."""
    common_dir = tmp_path / "mirror.git"
    git_dir = common_dir / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    commondir = git_dir / "commondir"
    commondir.write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    trusted_git_roots = (git_dir, common_dir)

    first = await make_worktree_activity_probe(
        worktree,
        trusted_git_roots=trusted_git_roots,
    )
    assert first is not None
    assert await first() is False

    agent_selected_common = tmp_path / "agent-selected-common"
    branch_ref = agent_selected_common / "refs" / "heads" / "awf" / "ws"
    branch_ref.parent.mkdir(parents=True)
    branch_ref.write_text("0" * 40 + "\n", encoding="utf-8")
    commondir.write_text(f"{agent_selected_common}\n", encoding="utf-8")

    second = await make_worktree_activity_probe(
        worktree,
        trusted_git_roots=trusted_git_roots,
    )
    assert second is not None
    branch_ref.write_text("1" * 40 + "\n", encoding="utf-8")
    assert await second() is None


@pytest.mark.unit
async def test_trusted_probe_fails_open_after_commondir_is_rewritten(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A post-prime rewrite cannot leave the managed branch watch stale."""
    common_dir = tmp_path / "mirror.git"
    git_dir = common_dir / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    commondir = git_dir / "commondir"
    commondir.write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    probe = await make_worktree_activity_probe(
        worktree,
        trusted_git_roots=(git_dir, common_dir),
    )
    assert probe is not None
    assert await probe() is False

    agent_selected_common = tmp_path / "agent-selected-common"
    branch_ref = agent_selected_common / "refs" / "heads" / "awf" / "ws"
    branch_ref.parent.mkdir(parents=True)
    branch_ref.write_text("0" * 40 + "\n", encoding="utf-8")
    commondir.write_text(f"{agent_selected_common}\n", encoding="utf-8")

    assert await probe() is None
    branch_ref.write_text("1" * 40 + "\n", encoding="utf-8")
    assert await probe() is None


@pytest.mark.unit
async def test_trusted_git_root_rejects_symlinked_commondir(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """The trusted check reads ``commondir`` without following a replacement."""
    common_dir = tmp_path / "mirror.git"
    git_dir = common_dir / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    agent_selected_common = tmp_path / "agent-selected-common"
    agent_selected_common.mkdir()
    (git_dir / "commondir").symlink_to(agent_selected_common, target_is_directory=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    probe = await make_worktree_activity_probe(
        worktree,
        trusted_git_roots=(git_dir, common_dir),
    )

    assert probe is not None
    assert await probe() is None


@pytest.mark.unit
async def test_trusted_git_root_rejects_symlinked_managed_path_component(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A lexical match cannot escape through a replaced mirror/worktrees dir."""
    common_dir = tmp_path / "mirror.git"
    common_dir.mkdir()
    untrusted = tmp_path / "agent-selected"
    git_dir_target = untrusted / "ws_probe"
    git_dir_target.mkdir(parents=True)
    (common_dir / "worktrees").symlink_to(untrusted, target_is_directory=True)
    expected_git_dir = common_dir / "worktrees" / "ws_probe"
    (worktree / ".git").write_text(
        f"gitdir: {expected_git_dir}\n",
        encoding="utf-8",
    )

    probe = await make_worktree_activity_probe(
        worktree,
        trusted_git_roots=(expected_git_dir, common_dir),
    )

    assert probe is not None
    assert await probe() is None


@pytest.mark.unit
def test_trusted_git_marker_rejects_lstat_open_replacement(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marker replaced between lstat and open cannot authorize the Git root."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    real_fstat = os.fstat

    def _changed_inode(descriptor: int) -> os.stat_result:
        opened = real_fstat(descriptor)
        fields = list(opened)
        fields[1] = opened.st_ino + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", _changed_inode)

    with pytest.raises(OSError, match="marker replaced"):
        worktree_activity._require_trusted_git_marker(worktree, git_dir)


@pytest.mark.unit
async def test_primed_probe_never_walks_replaced_git_admin_directory(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the pinned Git admin directory cannot redirect its walk."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(
        worktree,
        trusted_git_roots=(git_dir, tmp_path / "mirror.git"),
    )
    await probe.prime()
    assert await probe() is False

    original_git_dir = git_dir.with_name("ws_probe-original")
    git_dir.rename(original_git_dir)
    untrusted = tmp_path / "agent-selected"
    untrusted.mkdir()
    (untrusted / "sentinel").write_text("outside\n", encoding="utf-8")
    git_dir.symlink_to(untrusted, target_is_directory=True)
    real_scandir = os.scandir

    def _reject_replacement_walk(path: str | int) -> os.ScandirIterator[str]:
        if not isinstance(path, int):
            candidate = Path(path)
            if candidate in (git_dir, untrusted) or untrusted in candidate.parents:
                raise AssertionError(f"walk escaped through replaced Git dir: {candidate}")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _reject_replacement_walk)

    assert await probe() is None


@pytest.mark.unit
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_pinned_directory_open_rejects_stat_open_replacement(
    tmp_path: Path,
    replacement: str,
) -> None:
    """A root swapped after verification cannot win the no-follow open race."""
    root = tmp_path / "pinned.git"
    root.mkdir()
    pinned = worktree_activity._pin_directory(root)
    root.rename(tmp_path / "original.git")
    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    if replacement == "directory":
        replacement_root.rename(root)
    else:
        root.symlink_to(replacement_root, target_is_directory=True)

    with pytest.raises(OSError):
        worktree_activity._open_pinned_directory(pinned)


@pytest.mark.unit
def test_pinned_directory_capture_rejects_non_directory(tmp_path: Path) -> None:
    """Only a real directory may become a trusted external walk root."""
    root = tmp_path / "not-a-directory"
    root.write_text("gitdir data\n", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        worktree_activity._pin_directory(root)


@pytest.mark.unit
def test_observed_subdirectory_open_rejects_symlink_replacement(tmp_path: Path) -> None:
    """Descriptor-relative descent cannot follow a raced child symlink."""
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    observed = child.lstat()
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        child.rename(root / "original-child")
        child.symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(OSError):
            worktree_activity._open_observed_directory(
                child.name,
                descriptor,
                observed,
            )
    finally:
        os.close(descriptor)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("root_kind", "link_path", "watched_tail"),
    [
        ("git", Path("logs"), Path("HEAD")),
        ("git", Path("reftable"), Path("tables.list")),
        ("common", Path("refs") / "heads", Path("awf") / "ws"),
    ],
)
async def test_watched_git_path_rejects_symlinked_intermediate_directory(
    tmp_path: Path,
    worktree: Path,
    root_kind: str,
    link_path: Path,
    watched_tail: Path,
) -> None:
    """PRRT_kwDOSJAM6s6hRszr: watched metadata stays under pinned roots.

    ``Path.lstat`` protects only its final component. An agent can replace an
    intermediate ``logs``, ``reftable``, or ``refs/heads`` directory with a
    symlink and make an absolute-path lstat resolve outside the pinned Git root.
    The incomplete scan must fail open without following that replacement.
    """
    common_dir = tmp_path / "mirror.git"
    git_dir = common_dir / "worktrees" / "ws_probe"
    (git_dir / "logs").mkdir(parents=True)
    (git_dir / "reftable").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "logs" / "HEAD").write_text("reflog\n", encoding="utf-8")
    (git_dir / "reftable" / "tables.list").write_text("table.ref\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    branch_ref = common_dir / "refs" / "heads" / "awf" / "ws"
    branch_ref.parent.mkdir(parents=True)
    branch_ref.write_text("0" * 40 + "\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(common_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    watched_root = git_dir if root_kind == "git" else common_dir
    replaced = watched_root / link_path
    replaced.rename(replaced.with_name(f"{replaced.name}-original"))
    outside = tmp_path / f"outside-{root_kind}-{replaced.name}"
    outside_target = outside / watched_tail
    outside_target.parent.mkdir(parents=True)
    outside_target.write_text("agent-selected\n", encoding="utf-8")
    replaced.symlink_to(outside, target_is_directory=True)

    assert await probe() is None


@pytest.mark.unit
async def test_branch_resolution_rejects_symlinked_head(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """The branch watch must not follow a replaced HEAD while resolving it."""
    common_dir = tmp_path / "mirror.git"
    git_dir = common_dir / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    head = git_dir / "HEAD"
    head.write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(common_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    head.rename(git_dir / "HEAD-original")
    outside = tmp_path / "agent-selected-head"
    outside.write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    head.symlink_to(outside)

    assert await probe() is None


@pytest.mark.unit
async def test_worktree_walk_never_follows_root_replacement(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worktree root itself is opened without following a replacement."""
    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    original = tmp_path / "ws_probe-original"
    outside = tmp_path / "outside-root"
    outside.mkdir()
    (outside / "outside-root-sentinel").write_text("outside\n", encoding="utf-8")
    worktree.rename(original)
    worktree.symlink_to(outside, target_is_directory=True)
    real_entry_stat = worktree_activity._entry_stat

    def _reject_outside_entry(entry: os.DirEntry[str]) -> os.stat_result:
        if entry.name == "outside-root-sentinel":
            raise AssertionError("worktree scan escaped through replacement root")
        return real_entry_stat(entry)

    monkeypatch.setattr(worktree_activity, "_entry_stat", _reject_outside_entry)

    assert await probe() is None


@pytest.mark.unit
async def test_worktree_walk_never_follows_queued_directory_replacement(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory replaced after its lstat cannot redirect a later scan.

    Worktree descendants used to be queued without descriptors. If the agent
    renamed an observed directory and put an outside-pointing symlink at its
    path before that queue entry was popped, ``scandir(path)`` followed the
    replacement outside the isolated checkout.
    """
    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    source = worktree / "src"
    original = worktree / "src-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside-sentinel").write_text("outside\n", encoding="utf-8")
    real_entry_stat = worktree_activity._entry_stat
    armed = True

    def _replace_observed_directory(entry: os.DirEntry[str]) -> os.stat_result:
        nonlocal armed
        if entry.name == "outside-sentinel":
            raise AssertionError("worktree scan escaped through replacement symlink")
        stat_result = real_entry_stat(entry)
        if armed and entry.name == source.name:
            armed = False
            source.rename(original)
            source.symlink_to(outside, target_is_directory=True)
        return stat_result

    monkeypatch.setattr(worktree_activity, "_entry_stat", _replace_observed_directory)

    assert await probe() is None
    assert armed is False


@pytest.mark.unit
async def test_primed_probe_never_watches_replaced_commondir_target(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewritten ``commondir`` cannot redirect branch-ref metadata stats."""
    common_dir = tmp_path / "mirror.git"
    git_dir = common_dir / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    branch_ref = common_dir / "refs" / "heads" / "awf" / "ws"
    branch_ref.parent.mkdir(parents=True)
    branch_ref.write_text("0" * 40 + "\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(common_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    untrusted = tmp_path / "agent-selected-common"
    untrusted.mkdir()
    (git_dir / "commondir").write_text(f"{untrusted}\n", encoding="utf-8")
    real_lstat = Path.lstat

    def _reject_untrusted_stat(self: Path) -> os.stat_result:
        if self == untrusted or untrusted in self.parents:
            raise AssertionError(f"stat escaped to agent-selected path: {self}")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _reject_untrusted_stat)

    assert await probe() is True
    branch_ref.write_text("1" * 40 + "\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
@pytest.mark.parametrize("head_ref_kind", ["absolute", "traversal"])
async def test_rewritten_head_ref_cannot_escape_pinned_common_dir(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    head_ref_kind: str,
) -> None:
    """Agent-controlled HEAD text cannot redirect branch-ref metadata stats."""
    common_dir = tmp_path / "mirror.git"
    git_dir = common_dir / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(common_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    outside_ref = tmp_path / "outside-ref"
    head_ref = (
        str(outside_ref) if head_ref_kind == "absolute" else "refs/heads/../../../outside-ref"
    )
    (git_dir / "HEAD").write_text(f"ref: {head_ref}\n", encoding="utf-8")
    real_lstat = Path.lstat

    def _reject_external_stat(self: Path) -> os.stat_result:
        if Path(os.path.normpath(self)) == outside_ref:
            raise AssertionError(f"branch-ref stat escaped common dir: {self}")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _reject_external_stat)

    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_probe_without_pre_agent_layout_never_resolves_later_git_pointer(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed prime must not trust an external target first seen post-agent."""
    original_slots = worktree_activity._live_scan_threads
    monkeypatch.setattr(
        worktree_activity, "_live_scan_threads", worktree_activity._LiveScanThreads(0)
    )
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None
    monkeypatch.setattr(worktree_activity, "_live_scan_threads", original_slots)

    untrusted = tmp_path / "agent-selected-after-prime"
    untrusted.mkdir()
    (worktree / ".git").write_text(f"gitdir: {untrusted}\n", encoding="utf-8")

    def _reject_post_prime_resolution(_path: Path) -> None:
        raise AssertionError("post-prime Git metadata was resolved")

    monkeypatch.setattr(
        worktree_activity,
        "_resolve_git_layout",
        _reject_post_prime_resolution,
    )
    real_scandir = os.scandir

    def _reject_untrusted_walk(path: str) -> os.ScandirIterator[str]:
        candidate = Path(path)
        if candidate == untrusted or untrusted in candidate.parents:
            raise AssertionError(f"walk escaped to agent-selected path: {candidate}")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _reject_untrusted_walk)

    assert await probe() is None


@pytest.mark.unit
async def test_late_priming_resolution_cannot_publish_post_prime_git_target(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out resolver cannot publish metadata first observed after prime."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    gitfile = worktree / ".git"
    gitfile.write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    real_resolve = worktree_activity._resolve_git_layout
    started = threading.Event()
    release = threading.Event()

    def _stalled_resolve(path: Path) -> object:
        started.set()
        release.wait(timeout=10.0)
        return real_resolve(path)

    monkeypatch.setattr(worktree_activity, "_resolve_git_layout", _stalled_resolve)
    probe = WorktreeActivityProbe(worktree, prime_timeout_seconds=0.01)
    try:
        priming = asyncio.create_task(probe.prime())
        assert await asyncio.to_thread(started.wait, 10.0) is True
        assert await priming is True
        untrusted = tmp_path / "agent-selected-after-timeout"
        untrusted.mkdir()
        gitfile.write_text(f"gitdir: {untrusted}\n", encoding="utf-8")
    finally:
        release.set()

    await _await_scan_gate(probe)
    assert probe._git_layout_pinned is False
    assert await probe() is None


@pytest.mark.unit
async def test_failed_prime_still_scans_internal_git_directory(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real ``.git`` directory needs no post-prime external target resolution."""
    git_dir = worktree / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    _age_tree(worktree)

    original_slots = worktree_activity._live_scan_threads
    monkeypatch.setattr(
        worktree_activity, "_live_scan_threads", worktree_activity._LiveScanThreads(0)
    )
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None
    monkeypatch.setattr(worktree_activity, "_live_scan_threads", original_slots)

    (git_dir / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_entry_budget_stops_the_walk(worktree: Path) -> None:
    """A bounded walk gives up, answering "could not tell" rather than "idle".

    Failing closed here would idle-kill every healthy run in a worktree large
    enough to exhaust the budget — any ``node_modules`` / ``.venv`` does it on
    every probe — which is the #932 defect again. The wall timeout is what caps
    a genuinely wedged run.
    """
    probe = WorktreeActivityProbe(worktree, max_entries=1)

    with structlog.testing.capture_logs() as captured:
        assert await probe() is None

        for index in range(5):
            (worktree / "src" / "nested" / f"extra_{index}.py").write_text("z\n", encoding="utf-8")

        assert await probe() is None

    exhausted = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.entry_budget_exhausted"
    ]
    assert len(exhausted) == 2
    assert exhausted[0]["max_entries"] == 1


@pytest.mark.unit
async def test_unstattable_entry_is_never_reported_as_idle(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry the walk can list but not stat leaves the scan incomplete.

    Swallowing the error folded that entry into the fingerprint as a stable
    ``(path, None)`` term, so edits the agent (running as another user) makes to
    that existing file — which never move its parent directory's mtime — were
    invisible: consecutive scans matched and the watchdog would idle-kill a run
    that was still working. A directory whose stat fails is worse still, because
    nothing beneath it is ever walked.
    """
    real_scandir = os.scandir
    blocked = "src"

    class _UnstattableEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.path = entry.path
            self.name = entry.name

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            del follow_symlinks
            raise PermissionError("stat denied")

    class _PartiallyBrokenScandir:
        def __init__(self, path: str | int) -> None:
            self._inner = real_scandir(path)

        def __enter__(self) -> list[object]:
            return [
                _UnstattableEntry(entry) if entry.name == blocked else entry
                for entry in self._inner
            ]

        def __exit__(self, *_exc: object) -> None:
            self._inner.close()

    monkeypatch.setattr(os, "scandir", _PartiallyBrokenScandir)

    probe = WorktreeActivityProbe(worktree)
    with structlog.testing.capture_logs() as captured:
        assert await probe() is None
        assert await probe() is None

    unreadable = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.subtree_unreadable"
    ]
    assert len(unreadable) == 2
    assert unreadable[0]["path"] == str(worktree)


@pytest.mark.unit
async def test_unreadable_directory_reports_unknown(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory the walk cannot read leaves the scan incomplete, not idle."""

    def _deny(_path: str) -> object:
        raise PermissionError("scandir denied")

    monkeypatch.setattr(os, "scandir", _deny)

    probe = WorktreeActivityProbe(worktree)
    with structlog.testing.capture_logs() as captured:
        assert await probe() is None

    unreadable = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.subtree_unreadable"
    ]
    assert len(unreadable) == 1
    assert unreadable[0]["path"] == str(worktree)


@pytest.mark.unit
async def test_unreadable_subtree_is_never_reported_as_idle(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unreadable subtree must not yield a "complete" idle fingerprint.

    The worker may lack access to a directory the agent (running as another
    user) is still editing. Rewriting an existing file leaves its parent
    directory's mtime alone, so those writes are invisible everywhere the walk
    can see: consecutive fingerprints would match and the watchdog would
    idle-kill an actively editing agent — the #932 defect again.
    """
    real_open = worktree_activity._open_observed_directory

    def _deny_one(
        name: str,
        parent_descriptor: int,
        observed: os.stat_result,
    ) -> int:
        if name == "nested":
            raise PermissionError("scandir denied")
        return real_open(name, parent_descriptor, observed)

    monkeypatch.setattr(worktree_activity, "_open_observed_directory", _deny_one)

    probe = WorktreeActivityProbe(worktree)
    assert await probe() is None
    assert await probe() is None


@pytest.mark.unit
async def test_write_racing_the_walk_is_not_reported_as_idle(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file edited after the walk stat-ed it must not read as idleness.

    Writing to an existing regular file bumps only that file's mtime, not its
    parent directory's, so a write that lands mid-walk leaves the racing scan's
    fingerprint identical to the previous probe's. There is no "next probe" to
    report it: the watchdog fires the idle timeout the moment a probe answers
    "nothing moved", so it would kill a run that was writing during the probe —
    the #932 defect again.
    """
    target = worktree / "README.md"
    real_entry_stat = worktree_activity._entry_stat
    armed = False

    def _stat_then_race(entry: os.DirEntry[str]) -> os.stat_result:
        nonlocal armed
        stat_result = real_entry_stat(entry)
        if armed and entry.name == target.name:
            # Land the write *after* this entry was stat-ed, exactly once, so
            # the confirming rescan runs against a quiet tree.
            armed = False
            target.write_text("written mid-walk\n", encoding="utf-8")
        return stat_result

    monkeypatch.setattr(worktree_activity, "_entry_stat", _stat_then_race)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    armed = True
    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_truncated_confirming_rescan_answers_could_not_tell(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rescan that hits the entry budget has no opinion either — fail open.

    The complete scan stays the baseline, so the change that follows is still
    reported.
    """
    real_scan = WorktreeActivityProbe._scan
    scans = 0

    def _confirming_rescan_truncates(self: WorktreeActivityProbe) -> object:
        nonlocal scans
        # 1 primes the baseline, 2 is the probe's own scan, 3 is the rescan that
        # would confirm idleness.
        scans += 1
        return None if scans == 3 else real_scan(self)

    monkeypatch.setattr(WorktreeActivityProbe, "_scan", _confirming_rescan_truncates)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is None

    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_make_probe_returns_none_without_a_usable_path(tmp_path: Path) -> None:
    assert await make_worktree_activity_probe(None) is None
    assert await make_worktree_activity_probe(tmp_path / "missing") is None
    assert await make_worktree_activity_probe(tmp_path) is not None


@pytest.mark.unit
async def test_primed_probe_reports_a_quiet_worktree_as_idle(worktree: Path) -> None:
    """The pre-run baseline must not make an untouched worktree look busy."""
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None

    assert await probe() is False
    assert await probe() is False


@pytest.mark.unit
async def test_permission_change_before_the_first_probe_reports_activity(
    worktree: Path,
) -> None:
    """``chmod`` as the *only* activity in the first idle window is still activity.

    A clock-seeded first probe answers "idle" here — a mode change moves no
    mtime — and the confirming rescan then compares two identical post-``chmod``
    trees, so the watchdog would kill a run that was working. The mode term only
    helps if a fingerprint taken *before* the agent could act exists to differ
    from, which is what priming supplies.
    """
    script = worktree / "script.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    _age(script)
    _age(worktree)

    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None

    script.chmod(script.stat().st_mode | 0o111)

    assert await probe() is True


@pytest.mark.unit
async def test_truncated_priming_walk_falls_back_to_the_construction_seed(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Priming is best-effort: a truncated walk leaves the clock seed in charge.

    Newer-than-the-seed is still activity, and the first probe's own scan becomes
    the baseline every later probe compares fingerprints against.
    """
    _prime_scan_truncates(monkeypatch)

    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None

    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_seedless_first_probe_never_reports_idleness(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a baseline the first probe answers "could not tell", not "idle".

    A truncated priming walk leaves only the construction-time clock seed, and a
    clock cannot see a change that moves no mtime: a ``chmod`` in the first idle
    window looks exactly like a quiet worktree, and the confirming rescan cannot
    break the tie because both of its scans are post-``chmod``. Answering
    ``False`` there would idle-kill a working run — the #932 defect priming
    exists to prevent — so uncertainty fails open like any incomplete
    observation, and the scan just taken becomes the baseline.
    """
    script = worktree / "script.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    _age(script)
    _age(worktree)
    _prime_scan_truncates(monkeypatch)

    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None

    script.chmod(script.stat().st_mode | 0o111)

    assert await probe() is None
    assert await probe() is False


@pytest.mark.unit
async def test_failing_priming_walk_starts_the_run_without_a_baseline(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected priming error must not abort a run that has not started.

    Priming happens before the agent is launched, so letting anything the walk
    raises escape trades a best-effort optimisation for the whole run. It
    degrades to the documented seedless mode instead: no baseline, the
    construction seed in charge for one probe, and the failure logged.
    """

    def _boom(_self: WorktreeActivityProbe) -> object:
        raise ValueError("embedded null byte in path")

    monkeypatch.setattr(WorktreeActivityProbe, "_scan", _boom)

    with structlog.testing.capture_logs() as captured:
        probe = await make_worktree_activity_probe(worktree)

    assert probe is not None
    failures = [
        entry for entry in captured if entry.get("event") == "agent.worktree_activity.prime_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["exc_type"] == "ValueError"

    monkeypatch.undo()
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True
    assert await probe() is False
