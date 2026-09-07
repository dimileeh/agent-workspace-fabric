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
import os
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
