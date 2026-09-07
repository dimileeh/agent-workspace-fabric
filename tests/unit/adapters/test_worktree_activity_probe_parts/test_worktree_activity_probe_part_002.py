"""Worktree activity probe: walk uncertainty and priming (continued from part 001).

Uncertainty fails open: a walk that could not observe the whole tree — the entry
budget ran out, a directory was unreadable, or an entry could not be stat-ed —
answers ``None`` ("could not tell"), which the watchdog counts as activity,
never as idleness.

Split out of ``tests/unit/adapters/test_worktree_activity_probe.py`` to keep
each test module under the first-party 1500-line maintainability guardrail.
"""

from __future__ import annotations

import os
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
