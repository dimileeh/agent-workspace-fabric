"""Worktree activity probe backing the idle watchdog (issue #932).

"Liveness" for a print-mode agent is "the worktree moved", so the probe answers
one question: has anything under the workspace worktree changed since the last
probe? It excludes nothing the agent could legitimately write (``.git``
included) and, because a linked worktree keeps HEAD/index outside the worktree,
it also watches the resolved git dir's ``HEAD`` / ``index`` / ``logs/HEAD``.

Uncertainty fails open: a walk that could not observe the whole tree — the entry
budget ran out, a directory was unreadable, or an entry could not be stat-ed —
answers ``None`` ("could not tell"), which the watchdog counts as activity,
never as idleness.
"""

from __future__ import annotations

import asyncio
import concurrent.futures.thread
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import structlog

from awf.adapters import worktree_activity
from awf.adapters.worktree_activity import (
    WorktreeActivityProbe,
    make_worktree_activity_probe,
)

# The subprocess regression below must exercise the same source tree the rest of
# this module imports, not whatever ``awf`` a bare interpreter would resolve.
SRC_ROOT = Path(worktree_activity.__file__).parents[2]


def _age(path: Path, *, seconds: float = 3600.0) -> None:
    """Push ``path``'s mtime into the past so it predates any probe baseline."""
    stat_result = path.stat()
    os.utime(path, (stat_result.st_atime - seconds, stat_result.st_mtime - seconds))


def _age_tree(root: Path) -> None:
    for current, dirnames, filenames in os.walk(root):
        for name in filenames:
            _age(Path(current) / name)
        for name in dirnames:
            _age(Path(current) / name)
    _age(root)


async def _await_scan_gate(probe: WorktreeActivityProbe) -> None:
    """Wait for an abandoned scan's thread to settle, reopening the probe's gate.

    Only the thread's own completion reopens it — nothing can cancel a scan — so
    a test that releases a stalled walk and then probes has to let the released
    thread publish its result first.
    """
    for _ in range(500):
        if not probe._scan_gate.is_busy():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the abandoned scan never finished")


def _prime_scan_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the priming walk truncate, leaving the probe without a baseline."""
    real_scan = WorktreeActivityProbe._scan
    scans = 0

    def _first_scan_truncates(self: WorktreeActivityProbe) -> object:
        nonlocal scans
        scans += 1
        return None if scans == 1 else real_scan(self)

    monkeypatch.setattr(WorktreeActivityProbe, "_scan", _first_scan_truncates)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "ws_probe"
    (root / "src" / "nested").mkdir(parents=True)
    (root / "src" / "nested" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _age_tree(root)
    return root


@pytest.mark.unit
async def test_quiet_worktree_reports_no_activity(worktree: Path) -> None:
    probe = WorktreeActivityProbe(worktree)
    await probe.prime()

    assert await probe() is False
    assert await probe() is False


@pytest.mark.unit
async def test_modified_file_reports_activity_once(worktree: Path) -> None:
    """A single change is reported once; the baseline then advances."""
    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (worktree / "README.md").write_text("hello again\n", encoding="utf-8")

    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_future_dated_entry_does_not_blind_later_activity(worktree: Path) -> None:
    """A single future-stamped entry must not become a floor no write can clear.

    Tracking the tree as one maximum timestamp meant the first probe adopted the
    future stamp; every later write, stamped with the current clock, landed below
    it and read as idleness — one spurious extension, then an idle kill on a run
    that was still working. That is the #932 defect again.
    """
    future = worktree / "src" / "future.txt"
    future.write_text("from the future\n", encoding="utf-8")
    ahead = time.time() + 3600.0
    os.utime(future, (ahead, ahead))

    probe = WorktreeActivityProbe(worktree)
    assert await probe() is True

    for index in range(3):
        (worktree / "README.md").write_text(f"hello {index}\n", encoding="utf-8")
        assert await probe() is True
        assert await probe() is False


@pytest.mark.unit
async def test_created_file_in_nested_directory_reports_activity(worktree: Path) -> None:
    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (worktree / "src" / "nested" / "new_module.py").write_text("y = 2\n", encoding="utf-8")

    assert await probe() is True


@pytest.mark.unit
async def test_permission_change_reports_activity(worktree: Path) -> None:
    """``chmod +x`` moves no timestamp, size or inode — but Git sees the mode."""
    script = worktree / "script.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    _age(script)
    _age(worktree)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    script.chmod(script.stat().st_mode | 0o111)

    assert await probe() is True


@pytest.mark.unit
async def test_timestamp_preserving_rewrite_reports_activity(worktree: Path) -> None:
    """A same-length in-place rewrite with the mtime restored still changed the file.

    A timestamp-preserving formatter (or ``rsync --inplace --times``) leaves the
    path, mtime, size, inode and mode all identical while Git sees new content.
    ``st_ctime_ns`` is what moves — and the process cannot put it back — so it
    belongs in the fingerprint; without it a silent agent doing only such edits
    gets no extension and is killed at the idle deadline.
    """
    target = worktree / "README.md"
    _age(target)
    _age(worktree)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    before = target.stat()
    with target.open("r+b") as handle:
        handle.write(b"HELLO\n")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = target.stat()
    assert (after.st_mtime_ns, after.st_size, after.st_ino, after.st_mode) == (
        before.st_mtime_ns,
        before.st_size,
        before.st_ino,
        before.st_mode,
    )

    assert await probe() is True


@pytest.mark.unit
async def test_deleted_file_reports_activity(worktree: Path) -> None:
    """A delete only bumps the containing directory's mtime — dirs are stat-ed too."""
    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (worktree / "src" / "nested" / "module.py").unlink()

    assert await probe() is True


@pytest.mark.unit
async def test_linked_worktree_git_dir_head_and_index_are_watched(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """Only Git state moved: the gitfile-resolved git dir must still count."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    (git_dir / "logs").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "index").write_bytes(b"DIRC")
    (git_dir / "logs" / "HEAD").write_text("reflog\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "index").write_bytes(b"DIRC-updated")
    assert await probe() is True

    (git_dir / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    assert await probe() is True

    (git_dir / "logs" / "HEAD").write_text("reflog\nmore\n", encoding="utf-8")
    assert await probe() is True


def _linked_git_dir(tmp_path: Path, worktree: Path, *, head: str) -> Path:
    """Wire ``worktree`` up as a linked worktree of ``tmp_path/mirror.git``."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(head, encoding="utf-8")
    (git_dir / "index").write_bytes(b"DIRC")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    return git_dir


@pytest.mark.unit
async def test_commit_moving_only_the_branch_ref_reports_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A commit lands in ``refs/heads/<branch>`` under the *common* git dir.

    With ``core.logAllRefUpdates=false`` no reflog is written, the linked
    worktree's ``HEAD`` keeps naming the same branch, and an index that already
    matched the previous probe (staged during the preceding idle window) does
    not move either — so the three per-worktree files alone would report the
    commit as idleness and the watchdog would kill the agent moments after it
    committed.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    branch_ref = tmp_path / "mirror.git" / "refs" / "heads" / "awf" / "ws"
    branch_ref.parent.mkdir(parents=True)
    branch_ref.write_text("0" * 40 + "\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    branch_ref.write_text("1" * 40 + "\n", encoding="utf-8")
    assert await probe() is True
    assert (git_dir / "index").read_bytes() == b"DIRC"


@pytest.mark.unit
async def test_packed_branch_ref_absence_stays_a_complete_observation(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A packed ref has no loose file, and its later appearance is the commit."""
    _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    branch_ref = tmp_path / "mirror.git" / "refs" / "heads" / "awf" / "ws"
    branch_ref.parent.mkdir(parents=True)
    branch_ref.write_text("1" * 40 + "\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
@pytest.mark.parametrize("stack_dir", ["common", "worktree"])
async def test_reftable_commit_moving_only_the_stack_reports_activity(
    tmp_path: Path,
    worktree: Path,
    stack_dir: str,
) -> None:
    """Under ``extensions.refStorage=reftable`` there is no loose ref to watch.

    HEAD is a stub naming ``refs/heads/.invalid``, so the resolved branch ref
    never exists and its absence is a complete observation; the reflog and the
    already-staged index do not move either. Every ref transaction rewrites
    ``reftable/tables.list`` instead — in the common stack for a branch, in the
    worktree's own for a detached HEAD — so that is the only path left that a
    commit is guaranteed to move.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/.invalid\n")
    stack_root = git_dir if stack_dir == "worktree" else tmp_path / "mirror.git"
    stack = stack_root / "reftable" / "tables.list"
    stack.parent.mkdir(parents=True)
    stack.write_text("0x000000000001.ref\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    stack.write_text("0x000000000001.ref\n0x000000000002.ref\n", encoding="utf-8")
    assert await probe() is True
    assert (git_dir / "index").read_bytes() == b"DIRC"


@pytest.mark.unit
async def test_fetch_moving_only_fetch_head_reports_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """``git fetch --quiet`` moves nothing in the worktree and no watched ref.

    Its objects and remote-tracking refs land in the shared common dir, but
    ``FETCH_HEAD`` is per-worktree and every fetch rewrites it — so it is the
    one path in this worktree's own Git state that a quiet fetch is guaranteed
    to move. Left out, an interval whose only work was a fetch reads as
    idleness and the watchdog kills an agent that was doing repository work.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    fetch_head = git_dir / "FETCH_HEAD"
    fetch_head.write_text("0" * 40 + "\t\tbranch 'main' of origin\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    # Rewritten in place: the git dir's own mtime does not move, so only the
    # ``FETCH_HEAD`` watch can see this.
    fetch_head.write_text("1" * 40 + "\t\tbranch 'main' of origin\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_new_git_dir_metadata_file_reports_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """The git dir itself is watched, so any metadata file appearing counts.

    ``ORIG_HEAD``, ``MERGE_HEAD``, ``COMMIT_EDITMSG``, a ``rebase-merge`` dir —
    per-worktree Git state with no watch of its own. Creating one moves the
    containing git dir's mtime, which is the cheap way to cover the whole set.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "ORIG_HEAD").write_text("1" * 40 + "\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_rebase_step_inside_the_linked_git_dir_reports_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A linked worktree's git dir is walked, not just stat-ed by name.

    Its own mtime moves only when a *direct child* appears or is replaced, so a
    rebase advancing through an already existing ``rebase-merge`` directory —
    ``msgnum`` rewritten in place, a step appended to ``done`` — moved nothing
    the named watches or that mtime could see, while the worktree itself sits
    still between ``git rebase --continue`` invocations. That interval read as
    idleness and the watchdog killed an agent mid-rebase.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    rebase_merge = git_dir / "rebase-merge"
    rebase_merge.mkdir()
    (rebase_merge / "msgnum").write_text("1\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    # Same length, in place: neither the git dir nor ``rebase-merge`` itself
    # moves, so only walking the git dir can see this.
    (rebase_merge / "msgnum").write_text("2\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_vanished_linked_git_dir_still_allows_an_idle_answer(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A git dir gone by the time it is stat-ed is walked no further.

    Handing it to the walk anyway would turn its ``FileNotFoundError`` into
    "could not tell" on this scan and every later one — retiring the watchdog
    for the rest of the run — where absence is a complete observation the stat
    already folded in as a stable term.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    real_lstat = Path.lstat

    def _pruned(self: Path) -> os.stat_result:
        if self == git_dir or git_dir in self.parents:
            raise FileNotFoundError(2, "no such file", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _pruned)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False


@pytest.mark.unit
async def test_shared_common_dir_churn_is_not_reported_as_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """The common dir is never walked: its churn is other workspaces' agents.

    One bare mirror backs every worktree of a repo, so objects and remote refs
    landing there say nothing about *this* agent — walking it would report an
    idle run as alive whenever a neighbouring workspace fetched, and its object
    store would burn the walk budget. Only this worktree's own paths under it
    are watched by name.
    """
    _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    common_dir = tmp_path / "mirror.git"
    (common_dir / "objects" / "pack").mkdir(parents=True)
    _age_tree(worktree)
    _age_tree(common_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (common_dir / "objects" / "pack" / "pack-neighbour.pack").write_bytes(b"PACK")
    (common_dir / "refs" / "remotes" / "origin").mkdir(parents=True)
    (common_dir / "refs" / "remotes" / "origin" / "main").write_text(
        "1" * 40 + "\n",
        encoding="utf-8",
    )
    assert await probe() is False


@pytest.mark.unit
async def test_absent_reftable_stack_stays_a_complete_observation(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A files-backend repo has no ``reftable`` dir, and must still answer idle."""
    _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False


@pytest.mark.unit
@pytest.mark.parametrize("head", ["1" * 40 + "\n", "ref:\n", "ref: refs/heads/awf/ws\n"])
async def test_head_without_a_resolvable_branch_ref_still_answers_idle(
    tmp_path: Path,
    worktree: Path,
    head: str,
) -> None:
    """A detached or empty HEAD names no ref — a complete observation, not ``None``.

    The last case keeps a *named* branch whose common dir cannot be reached
    through a ``commondir`` that is simply absent: the git dir is then the
    common one, exactly as for a plain checkout behind a ``.git`` symlink.
    """
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(head, encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
@pytest.mark.parametrize("commondir", ["\n", "{absolute}\n"])
async def test_commondir_forms_resolve_to_the_same_branch_ref(
    tmp_path: Path,
    worktree: Path,
    commondir: str,
) -> None:
    """An empty ``commondir`` falls back to the git dir; an absolute one is used as is."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    common = tmp_path / "mirror.git" if "{absolute}" in commondir else git_dir
    (git_dir / "commondir").write_text(
        commondir.format(absolute=tmp_path / "mirror.git"),
        encoding="utf-8",
    )
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    branch_ref = common / "refs" / "heads" / "awf" / "ws"
    branch_ref.parent.mkdir(parents=True)
    branch_ref.write_text("1" * 40 + "\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
@pytest.mark.parametrize("denied_name", ["HEAD", "commondir"])
async def test_unreadable_ref_resolution_input_reports_unknown(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    denied_name: str,
) -> None:
    """Which branch a commit lands in is unknowable, so the scan is incomplete.

    Falling back to "no branch ref" would drop the one path a reflog-less commit
    moves from an otherwise complete-looking fingerprint — an idle kill of an
    agent mid-commit.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    real_read_text = Path.read_text
    denied = git_dir / denied_name

    def _deny_one(self: Path, *args: object, **kwargs: object) -> str:
        if self == denied:
            raise PermissionError(13, "read denied", str(denied))
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _deny_one)

    probe = WorktreeActivityProbe(worktree)
    with structlog.testing.capture_logs() as captured:
        assert await probe() is None

    unreadable = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.git_pointer_unreadable"
    ]
    assert len(unreadable) == 1
    assert unreadable[0]["path"] == str(denied)


@pytest.mark.unit
async def test_relative_gitdir_pointer_resolves_against_the_worktree(
    tmp_path: Path,
    worktree: Path,
) -> None:
    git_dir = tmp_path / "relative.git"
    git_dir.mkdir()
    (git_dir / "index").write_bytes(b"DIRC")
    relative = os.path.relpath(git_dir, worktree)
    (worktree / ".git").write_text(f"gitdir: {relative}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "index").write_bytes(b"DIRC-updated")
    assert await probe() is True


@pytest.mark.unit
async def test_unreadable_git_dir_metadata_reports_unknown(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metadata path that cannot be stat-ed leaves the scan incomplete.

    HEAD / index / logs/HEAD live outside a linked worktree, so they are the
    only place Git-only activity shows up. Folding an unreadable one in as a
    stable ``(path, None)`` term claimed a complete scan while blind to every
    later commit or index write — consecutive fingerprints matched and the
    watchdog would idle-kill a run that was still working.
    """
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "index").write_bytes(b"DIRC")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    real_lstat = Path.lstat
    denied = git_dir / "index"

    def _deny_one(self: Path) -> os.stat_result:
        if self == denied:
            raise PermissionError("lstat denied")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _deny_one)

    probe = WorktreeActivityProbe(worktree)
    with structlog.testing.capture_logs() as captured:
        assert await probe() is None
        assert await probe() is None

    unreadable = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.metadata_unreadable"
    ]
    assert len(unreadable) == 2
    assert unreadable[0]["path"] == str(denied)


@pytest.mark.unit
async def test_absent_git_dir_metadata_still_allows_an_idle_answer(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A metadata path that is simply not there is a complete observation.

    ``logs/HEAD`` only exists once a reflog does, so treating its absence as
    "could not tell" would wedge the probe into answering ``None`` forever and
    the idle watchdog would never fire at all. Its later appearance is activity.
    """
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "logs").mkdir()
    (git_dir / "logs" / "HEAD").write_text("reflog\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_unreadable_gitfile_pointer_reports_unknown(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``.git`` pointer this process cannot read leaves the scan incomplete.

    The control plane and the agent run as different users, so the pointer may
    be unreadable here while the agent keeps committing through it. Treating
    that like a plain (non-linked) checkout drops HEAD / index / logs/HEAD from
    the scan and still claims a complete fingerprint — Git-only activity would
    then leave consecutive fingerprints equal and authorise an idle kill.
    """
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "index").write_bytes(b"DIRC")
    gitfile = worktree / ".git"
    gitfile.write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    real_read_text = Path.read_text

    def _deny_gitfile(self: Path, *args: object, **kwargs: object) -> str:
        if self == gitfile:
            raise PermissionError("read denied")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _deny_gitfile)

    probe = WorktreeActivityProbe(worktree)
    with structlog.testing.capture_logs() as captured:
        assert await probe() is None
        assert await probe() is None

    unreadable = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.git_pointer_unreadable"
    ]
    assert len(unreadable) == 2
    assert unreadable[0]["path"] == str(gitfile)


@pytest.mark.unit
@pytest.mark.parametrize("gitfile_body", ["", "gitdir:\n", "not a gitfile\n"])
async def test_unusable_gitfile_falls_back_to_the_worktree_walk(
    worktree: Path,
    gitfile_body: str,
) -> None:
    (worktree / ".git").write_text(gitfile_body, encoding="utf-8")
    _age_tree(worktree)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True


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
    blocked = str(worktree / "src")

    class _UnstattableEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.path = entry.path
            self.name = entry.name

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            del follow_symlinks
            raise PermissionError("stat denied")

    class _PartiallyBrokenScandir:
        def __init__(self, path: str) -> None:
            self._inner = real_scandir(path)

        def __enter__(self) -> list[object]:
            return [
                _UnstattableEntry(entry) if entry.path == blocked else entry
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
    real_scandir = os.scandir
    blocked = str(worktree / "src" / "nested")

    def _deny_one(path: str) -> object:
        if path == blocked:
            raise PermissionError("scandir denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _deny_one)

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
        if armed and entry.path == str(target):
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


@pytest.mark.unit
async def test_scans_never_occupy_the_shared_default_executor(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every scan runs in the probe's own pool, never the interpreter-wide one.

    A stalled ``scandir`` cannot be cancelled, and every caller here abandons the
    wait under a timeout, so the thread keeps running until the filesystem
    answers. In the default ``asyncio.to_thread`` executor those abandoned
    threads accumulate against the fixed worker count the rest of the control
    plane draws from — git, Docker and GC work would queue behind a worktree the
    agent's own timeouts already gave up on.
    """
    scan_threads: list[str] = []
    real_scan = WorktreeActivityProbe._scan

    def _record_thread(self: WorktreeActivityProbe) -> object:
        scan_threads.append(threading.current_thread().name)
        return real_scan(self)

    monkeypatch.setattr(WorktreeActivityProbe, "_scan", _record_thread)

    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None
    assert await probe() is False

    prefix = worktree_activity._SCAN_THREAD_NAME_PREFIX
    # Priming, the probe scan and its confirming rescan.
    assert len(scan_threads) == 3
    assert all(name.startswith(prefix) for name in scan_threads)
    # The pools really are distinct: the shared one hands out other threads.
    shared_thread = await asyncio.to_thread(lambda: threading.current_thread().name)
    assert not shared_thread.startswith(prefix)


@pytest.mark.unit
async def test_scans_run_on_threads_that_skip_interpreter_shutdown(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scan threads are daemons, and no executor joins them on the way out.

    An abandoned scan keeps running until the filesystem answers. A
    ``ThreadPoolExecutor`` worker holding it would be joined at interpreter
    shutdown — its ``atexit`` hook waits for every worker — so a graceful worker
    restart after any stalled scan would hang forever, long after the agent's own
    timeout already gave up. A daemon thread of our own is joined by nobody.
    """
    scan_threads: list[threading.Thread] = []
    real_scan = WorktreeActivityProbe._scan

    def _record_thread(self: WorktreeActivityProbe) -> object:
        scan_threads.append(threading.current_thread())
        return real_scan(self)

    monkeypatch.setattr(WorktreeActivityProbe, "_scan", _record_thread)

    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None
    assert await probe() is False

    assert scan_threads
    assert all(thread.daemon for thread in scan_threads)
    # Not an executor worker: those are exactly the threads ``_python_exit``
    # joins while the interpreter is trying to shut down.
    assert all(thread not in concurrent.futures.thread._threads_queues for thread in scan_threads)


@pytest.mark.unit
async def test_scan_abandoned_before_its_thread_starts_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that gives up before the thread runs leaves nothing to raise in it.

    ``wrap_future`` cancels the pending result on the way out, so a thread that
    went on to publish into it would die with ``InvalidStateError`` on a stray
    scan nobody is waiting for. It skips the walk instead. Nothing was left
    running either, so this is not the leak the warning below is about.
    """
    pending: list[object] = []
    monkeypatch.setattr(worktree_activity, "_start_scan_thread", pending.append)
    walked = False

    def _work() -> str:
        nonlocal walked
        walked = True
        return "scanned"

    with structlog.testing.capture_logs() as captured:
        scan = asyncio.ensure_future(
            worktree_activity._run_scan(
                _work,
                worktree_path="/ws/never-started",
                gate=worktree_activity._ScanGate(),
            )
        )
        await asyncio.sleep(0)
        scan.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scan
        await asyncio.sleep(0)

    assert len(pending) == 1
    deliver = pending[0]
    assert callable(deliver)
    deliver()  # The thread the real starter would have run, late.
    assert walked is False
    assert not [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.scan_abandoned"
    ]


@pytest.mark.unit
async def test_abandoning_a_stalled_scan_names_the_worktree_that_leaked_the_thread(
    worktree: Path,
) -> None:
    """Giving up on a running scan is recorded, so the leak is not silent.

    Nothing can reclaim a daemon thread parked on a ``scandir`` the filesystem
    never answers — abandoning the wait is the only escape, and the thread is
    left behind. Every caller then reports "could not tell", which the watchdog
    counts as activity, so a permanently stalled worktree is indistinguishable
    from a busy agent while it sheds one thread per idle window. The warning
    naming the worktree is the only thing that tells an operator which workspace
    is doing it.
    """
    started = threading.Event()
    release = threading.Event()

    def _stalled() -> str:
        started.set()
        release.wait(timeout=10.0)
        return "scanned"

    try:
        with structlog.testing.capture_logs() as captured:
            scan = asyncio.ensure_future(
                worktree_activity._run_scan(
                    _stalled,
                    worktree_path=str(worktree),
                    gate=worktree_activity._ScanGate(),
                )
            )
            await asyncio.to_thread(started.wait, 10.0)
            scan.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scan
    finally:
        release.set()

    abandoned = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.scan_abandoned"
    ]
    assert len(abandoned) == 1
    assert abandoned[0]["worktree_path"] == str(worktree)
    assert abandoned[0]["log_level"] == "warning"


@pytest.mark.unit
async def test_a_scan_that_answers_in_time_is_not_reported_as_abandoned(
    worktree: Path,
) -> None:
    """The warning marks leaked threads only; ordinary probes stay quiet."""
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None

    with structlog.testing.capture_logs() as captured:
        assert await probe() is False

    assert not [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.scan_abandoned"
    ]


@pytest.mark.unit
async def test_probe_starts_no_second_thread_while_a_scan_is_still_running(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unreclaimable scan thread per worktree is the whole budget.

    An abandoned scan runs until the filesystem answers, and the probe fails
    open, so a wedged worktree is indistinguishable from a busy agent: without
    this bound it would shed another thread nothing can join every idle window,
    forever on a run with no wall timeout, until the worker's thread/PID limits
    stop unrelated agents from running. The gated probe answers "could not tell"
    instead — exactly what the stalled scan itself was going to say.
    """
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None
    started = threading.Event()
    release = threading.Event()
    real_scan = probe._scan
    scans = 0
    before = set(threading.enumerate())

    def _first_scan_stalls() -> object:
        nonlocal scans
        scans += 1
        if scans == 1:
            started.set()
            release.wait(timeout=10.0)
        return real_scan()

    monkeypatch.setattr(probe, "_scan", _first_scan_stalls)

    try:
        stalled = asyncio.ensure_future(probe())
        await asyncio.to_thread(started.wait, 10.0)
        stalled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stalled

        with structlog.testing.capture_logs() as captured:
            assert await probe() is None
    finally:
        release.set()

    assert scans == 1
    gated = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.scan_still_running"
    ]
    assert len(gated) == 1
    assert gated[0]["worktree_path"] == str(worktree)
    assert gated[0]["log_level"] == "warning"
    fresh = [
        thread
        for thread in threading.enumerate()
        if thread not in before and thread.name.startswith("awf-worktree-scan")
    ]
    assert len(fresh) == 1


@pytest.mark.unit
async def test_probe_scans_again_once_the_abandoned_thread_finishes(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is one *live* thread, not one scan: the slot reopens.

    A filesystem that unwedges must put the probe back to work — a gate that
    latched would leave the watchdog reading "could not tell" as activity for
    the rest of the run and turn a transient stall into a permanently blind
    idle timeout.
    """
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None
    started = threading.Event()
    release = threading.Event()
    real_scan = probe._scan
    scans = 0

    def _first_scan_stalls() -> object:
        nonlocal scans
        scans += 1
        if scans == 1:
            started.set()
            release.wait(timeout=10.0)
        return real_scan()

    monkeypatch.setattr(probe, "_scan", _first_scan_stalls)

    stalled = asyncio.ensure_future(probe())
    await asyncio.to_thread(started.wait, 10.0)
    stalled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stalled
    release.set()
    await _await_scan_gate(probe)

    # The abandoned scan's own result was discarded, so the pre-run baseline is
    # still what this probe compares against.
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_probe_starts_no_thread_once_the_worker_wide_ceiling_is_full(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-worktree gates know nothing about each other; the worker bounds them all.

    Each wedged workspace parks one thread nothing can reclaim, and its own gate
    is satisfied — so enough of them still exhaust the worker's thread/PID limits
    and stop unrelated agents from running. A scan with no slot left starts no
    thread at all and fails open like a gated one.
    """
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None
    full = worktree_activity._LiveScanThreads(1)
    assert full.acquire() is True  # Stands in for another worktree's stalled scan.
    monkeypatch.setattr(worktree_activity, "_live_scan_threads", full)
    before = set(threading.enumerate())

    with structlog.testing.capture_logs() as captured:
        assert await probe() is None

    refused = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.scan_capacity_exhausted"
    ]
    assert len(refused) == 1
    assert refused[0]["worktree_path"] == str(worktree)
    assert refused[0]["max_live_scan_threads"] == 1
    assert refused[0]["log_level"] == "warning"
    assert not [
        thread
        for thread in threading.enumerate()
        if thread not in before and thread.name.startswith("awf-worktree-scan")
    ]

    # Refusing must not latch: the freed slot puts the probe back to work, and
    # its own gate is untouched, so the pre-run baseline still applies.
    full.release()
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_finished_scans_return_their_worker_wide_slot(worktree: Path) -> None:
    """The ceiling counts *live* threads, so ordinary probing cannot drain it.

    A slot leaked per completed scan would wedge every worktree on the worker
    after a few hundred quiet idle windows — the watchdog reading "could not
    tell" as activity for the rest of every run.
    """
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None

    assert await probe() is False
    assert await probe() is False

    assert worktree_activity._live_scan_threads._live == 0


@pytest.mark.unit
async def test_priming_without_a_worker_wide_slot_still_starts_the_run(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full ceiling is one more missing baseline, never a refused agent launch.

    Priming is best-effort: dropping the probe here would put the watchdog back
    on the stdout-only cap that kills healthy print-mode runs (#932).
    """
    full = worktree_activity._LiveScanThreads(0)
    monkeypatch.setattr(worktree_activity, "_live_scan_threads", full)

    with structlog.testing.capture_logs() as captured:
        probe = await make_worktree_activity_probe(worktree)

    assert probe is not None
    failures = [
        entry for entry in captured if entry.get("event") == "agent.worktree_activity.prime_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["exc_type"] == "_ScanCapacityError"

    # Seedless degraded mode, exactly like any other priming failure.
    monkeypatch.undo()
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_a_thread_that_cannot_start_frees_the_slot_it_reserved(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Thread.start`` can still fail below the ceiling; that must not latch.

    Nothing runs the deliver callback in that case, so the reserved slot and the
    worktree's own gate have to be released here — otherwise one transient
    thread exhaustion would blind this probe, and leak a slot from the worker's
    budget, for the rest of the process.
    """
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None

    def _cannot_start(_deliver: object) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(worktree_activity, "_start_scan_thread", _cannot_start)
    assert await probe() is None

    monkeypatch.undo()
    assert worktree_activity._live_scan_threads._live == 0
    assert probe._scan_gate.is_busy() is False
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
def test_abandoned_scan_does_not_block_interpreter_shutdown(tmp_path: Path) -> None:
    """A stalled scan the caller gave up on must not wedge process shutdown.

    The control plane owns worker lifecycle: a restart has to complete even when
    a worktree's filesystem never answers. This runs the abandoned-scan case in a
    real interpreter and requires it to exit, rather than hanging in the shutdown
    join a pooled worker would sit in.
    """
    worktree = tmp_path / "ws_shutdown"
    worktree.mkdir()
    script = textwrap.dedent(
        f"""
        import asyncio, sys, threading
        from pathlib import Path
        sys.path.insert(0, {str(SRC_ROOT)!r})
        from awf.adapters.worktree_activity import WorktreeActivityProbe

        stalled = threading.Event()

        def _stalled_scan(_self):
            stalled.wait(timeout=300.0)
            return None

        WorktreeActivityProbe._scan = _stalled_scan

        async def main():
            probe = WorktreeActivityProbe(
                Path({str(worktree)!r}), prime_timeout_seconds=0.05
            )
            assert await probe.prime() is True

        asyncio.run(main())
        print("abandoned")
        """,
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell.
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "abandoned" in completed.stdout


@pytest.mark.unit
async def test_stalled_priming_walk_is_capped_rather_than_wedging_the_worker(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Priming runs outside the run's wall budget, so it carries its own cap.

    The scan sits in a thread that cannot be interrupted from the event loop, so
    an unbounded await on a stalled ``scandir`` / ``stat`` would park the worker
    with no deadline left to escape through. The wait is abandoned instead — the
    thread finishes on its own — leaving the same seedless degraded mode.
    """
    release = threading.Event()

    def _stalled(_self: WorktreeActivityProbe) -> object:
        release.wait(timeout=30.0)
        return None

    monkeypatch.setattr(WorktreeActivityProbe, "_scan", _stalled)

    probe = WorktreeActivityProbe(worktree, prime_timeout_seconds=0.01)
    try:
        with structlog.testing.capture_logs() as captured:
            assert await probe.prime() is True
    finally:
        release.set()

    failures = [
        entry for entry in captured if entry.get("event") == "agent.worktree_activity.prime_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["prime_timeout_seconds"] == 0.01

    monkeypatch.undo()
    # The abandoned priming thread holds the probe's one scan slot until it
    # finishes; probing before then is gated, not seedless.
    await _await_scan_gate(probe)
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_stalled_existence_check_is_capped_like_the_priming_walk(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "anything to watch?" check is a ``stat``, so it runs under the same cap.

    On the event loop it would block the worker on a stalled worktree filesystem
    *before* the priming timeout could arm: the agent never starts, and neither
    the priming cap nor the run's wall deadline is reachable to recover it. It
    therefore sits in the bounded worker thread with the walk, and a stall
    degrades to the seedless mode rather than dropping the probe — without it the
    watchdog is back to the stdout-only cap that kills healthy print-mode runs.
    """
    release = threading.Event()
    real_exists = Path.exists

    def _stalled_exists(self: Path, **kwargs: object) -> bool:
        if self == worktree:
            release.wait(timeout=5.0)
        return bool(real_exists(self, **kwargs))

    monkeypatch.setattr(Path, "exists", _stalled_exists)

    building = asyncio.ensure_future(make_worktree_activity_probe(worktree))
    started = time.monotonic()
    try:
        for _ in range(5):
            await asyncio.sleep(0.01)
        # The loop kept running while the stalled check was in flight; done on
        # the loop instead, it would hold every deadline until the stat returned.
        assert time.monotonic() - started < 1.0
        assert not building.done()
    finally:
        release.set()
    probe = await building

    assert probe is not None
    monkeypatch.undo()
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_unreadable_worktree_path_starts_the_run_without_a_baseline(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existence check that *raises* must not abort a run that has not started.

    ``Path.exists`` only swallows "not there" errors; ``EACCES`` / ``EIO`` escape
    it. Letting that abort the launch would trade the whole run for a best-effort
    optimisation, so it lands in the same seedless degraded mode as any other
    priming failure — with the failure logged.
    """
    real_exists = Path.exists

    def _unreadable_exists(self: Path, **kwargs: object) -> bool:
        if self == worktree:
            raise PermissionError("worktree is not readable by the worker")
        return bool(real_exists(self, **kwargs))

    monkeypatch.setattr(Path, "exists", _unreadable_exists)

    with structlog.testing.capture_logs() as captured:
        probe = await make_worktree_activity_probe(worktree)

    assert probe is not None
    failures = [
        entry for entry in captured if entry.get("event") == "agent.worktree_activity.prime_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["exc_type"] == "PermissionError"

    monkeypatch.undo()
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True
    assert await probe() is False
