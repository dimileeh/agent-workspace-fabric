"""Worktree activity probe for the agent idle watchdog (issue #932).

The idle watchdog in ``awf.common.commands`` only ever saw the child's
stdout/stderr. Claude Code runs with ``-p`` (print mode) and emits nothing until
it finishes, so ``agent_idle_timeout_seconds`` degraded into a blind cap that
killed healthy hour-long runs. Liveness for a coding agent is "the worktree
moved", so this module supplies the ``ActivityProbe`` the watchdog consults when
the idle deadline is reached: has anything under the workspace worktree changed
since the previous probe?

Design notes:

* Nothing is excluded. The agent may legitimately write anywhere in its
  worktree, ``.git`` included.
* No marker file is written. A marker inside the worktree would show up as dirty
  residue in the verdict / dirty-sink fingerprints, and shelling out to
  ``find(1)`` would add a dependency on the control-plane image's toolchain.
* A *linked* worktree keeps HEAD/index outside the tree (``.git`` is a
  ``gitdir:`` pointer file), so "the index or HEAD moved" is only observable by
  also stat-ing the resolved git dir's ``HEAD`` / ``index`` / ``logs/HEAD``.
* Change is detected by comparing a **fingerprint of the whole tree** — every
  entry's path, mtime, size, inode and mode, combined order-independently — against
  the previous probe's, not by tracking one newest mtime. A single maximum
  timestamp is blinded by a single future-dated entry: the first probe would
  adopt that stamp as the floor, and every later write, stamped with the current
  clock, would land below it and read as idleness. That is one spurious
  extension followed by an idle kill of a run that is still working — the #932
  defect again. A fingerprint also has no skew against the kernel's coarse inode
  clock.
* An unchanged fingerprint is confirmed by an immediate **rescan** before it is
  reported as idleness. A write to an existing regular file bumps only that
  file's mtime, not its parent directory's, so a write landing after the walk
  already stat-ed that entry leaves no trace in the scan it raced. There is no
  "next probe" to report it either: the watchdog kills the child the moment a
  probe answers "nothing moved". The rescan starts after the raced walk ended,
  so it stats that file *after* the write and reports the change.
* Only the *first* probe has nothing to compare against, so it alone is
  clock-based: it asks whether anything is newer than a seed taken when the
  probe was built, with a small tolerance for that coarse-clock lag.
* The walk is bounded by an entry budget, and running out fails **open**: the
  probe reports ``None`` ("could not tell"), which the watchdog counts as
  activity. A truncated walk has no opinion about liveness, and any worktree
  with a ``node_modules`` / ``.venv`` in it exhausts the budget on every probe,
  so failing closed there would idle-kill every healthy run in such a
  repository — the #932 defect again. The wall timeout remains the hard cap.
* A directory the walk cannot read — or an entry it can list but cannot stat —
  fails open the same way. The worker and the agent run as different users, so a
  subtree the agent is still editing may be unreadable here; skipping it and
  returning the rest as a complete fingerprint would report those writes as
  idleness, because rewriting an existing file never moves its parent
  directory's mtime. An entry that cannot be stat-ed is worse still: it folds
  into the fingerprint as a stable term and, if it is a directory, is never
  descended into, so nothing under it is ever observed. Any incomplete
  observation is ``None``, not ``False``.
"""

from __future__ import annotations

import asyncio
import os
import stat as stat_module
import time
from pathlib import Path
from typing import NamedTuple

from awf.common.commands import ActivityProbe
from awf.common.logging import get_logger

_log = get_logger(__name__)

# Bounded so one probe can never walk an unbounded tree on the worker.
DEFAULT_MAX_ENTRIES = 200_000

# Slack for the kernel's coarse inode-timestamp clock lagging ``time.time()``.
# Only ever applied to the seed, which only the first probe consults; at worst
# it grants one extra idle window to a run whose worktree was touched moments
# before it started.
_COARSE_CLOCK_TOLERANCE_SECONDS = 0.05

# Fingerprint terms are summed, so the walk order cannot change the result.
_FINGERPRINT_MASK = (1 << 64) - 1

_GITDIR_PREFIX = "gitdir:"
# Files that move when only Git state changed in a linked worktree.
_GIT_DIR_ACTIVITY_FILES = (Path("HEAD"), Path("index"), Path("logs") / "HEAD")


class _Scan(NamedTuple):
    """One complete observation of the worktree."""

    newest_mtime: float
    fingerprint: int


class WorktreeActivityProbe:
    """Report whether anything under a worktree changed since the last probe."""

    def __init__(
        self,
        worktree_path: Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._worktree_path = worktree_path
        self._max_entries = max_entries
        self._previous: _Scan | None = None
        # Wall clock, because ``st_mtime`` is wall clock, and only ever read by
        # the first probe. Never compared against the event loop's monotonic
        # clock — the probe only returns a boolean.
        self._seed = time.time() - _COARSE_CLOCK_TOLERANCE_SECONDS

    async def __call__(self) -> bool | None:
        """Scan off the event loop and compare against what the last probe saw.

        Returns ``None`` when the walk was truncated: the scan saw part of the
        tree and cannot claim the worktree was idle. The remembered scan is left
        alone in that case, so a later complete scan still reports the change it
        missed.

        A scan that observed no change is not yet proof of idleness — a write
        can race the walk — so it is confirmed by a rescan before answering
        ``False``.
        """
        scan = await asyncio.to_thread(self._scan)
        if scan is None:
            return None
        previous, self._previous = self._previous, scan
        if self._observed_change(previous, scan):
            return True
        return await self._confirm_idle(scan)

    def _observed_change(self, previous: _Scan | None, scan: _Scan) -> bool:
        if previous is None:
            # Nothing observed yet, so the construction-time seed is the only
            # reference point this one probe has.
            return scan.newest_mtime > self._seed
        return scan.fingerprint != previous.fingerprint

    async def _confirm_idle(self, scan: _Scan) -> bool | None:
        """Rescan, because a write can race a walk without changing its result.

        Modifying an existing regular file leaves its parent directory's mtime
        alone, so a write landing after the walk stat-ed that entry is invisible
        to the scan it raced. A negative answer is final — the watchdog fires the
        idle timeout on it rather than probing again — so the scan is repeated
        before "nothing moved" is believed. The rescan begins after the raced
        walk finished and therefore stats that entry after the write.
        """
        confirm = await asyncio.to_thread(self._scan)
        if confirm is None:
            # Same fail-open rule as any truncated walk: no opinion, and the
            # complete scan stays the baseline for the next probe.
            return None
        self._previous = confirm
        return confirm.fingerprint != scan.fingerprint

    def _scan(self) -> _Scan | None:
        """Fingerprint the worktree, or ``None`` if the walk was truncated."""
        newest = 0.0
        fingerprint = 0
        for path in (self._worktree_path, *self._git_dir_paths()):
            newest, fingerprint = _absorb(newest, fingerprint, str(path), _stat_or_none(path))
        stack: list[str] = [str(self._worktree_path)]
        budget = self._max_entries
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if budget <= 0:
                            _log.warning(
                                "agent.worktree_activity.entry_budget_exhausted",
                                worktree_path=str(self._worktree_path),
                                max_entries=self._max_entries,
                            )
                            return None
                        budget -= 1
                        # Directories count too: a create / delete / rename only
                        # bumps the containing directory. One lstat answers both
                        # questions, and its failure propagates to the handler
                        # below rather than being folded in as a stable term.
                        stat_result = _entry_stat(entry)
                        newest, fingerprint = _absorb(
                            newest,
                            fingerprint,
                            entry.path,
                            stat_result,
                        )
                        if stat_module.S_ISDIR(stat_result.st_mode):
                            stack.append(entry.path)
            except OSError as exc:
                # An unreadable directory — or an entry that can be listed but
                # not stat-ed — means the walk did not observe the whole tree,
                # so the fingerprint it would return is not the complete one it
                # claims to be. Writes to existing files inside that subtree
                # leave no trace anywhere the walk *can* see — a directory's
                # mtime does not move when a file inside it is rewritten — so
                # consecutive scans would match and the watchdog would idle-kill
                # an agent that is still editing. Same fail-open rule as the
                # entry budget: no opinion, and the remembered scan is left
                # alone.
                _log.warning(
                    "agent.worktree_activity.subtree_unreadable",
                    worktree_path=str(self._worktree_path),
                    path=current,
                    error=str(exc),
                )
                return None
        return _Scan(newest_mtime=newest, fingerprint=fingerprint)

    def _git_dir_paths(self) -> tuple[Path, ...]:
        git_dir = _resolve_linked_git_dir(self._worktree_path)
        if git_dir is None:
            return ()
        return tuple(git_dir / name for name in _GIT_DIR_ACTIVITY_FILES)


def make_worktree_activity_probe(worktree_path: Path | None) -> ActivityProbe | None:
    """Build a probe for ``worktree_path``, or ``None`` when there is nothing to watch."""
    if worktree_path is None or not worktree_path.exists():
        return None
    return WorktreeActivityProbe(worktree_path)


def _absorb(
    newest: float,
    fingerprint: int,
    path: str,
    stat_result: os.stat_result | None,
) -> tuple[float, int]:
    """Fold one path into the running newest mtime and tree fingerprint."""
    if stat_result is None:
        # A path we cannot stat still contributes its identity, so that its
        # appearance or disappearance registers as a change.
        term = hash((path, None))
    else:
        newest = max(newest, stat_result.st_mtime)
        # Inode and size make a same-size atomic replace within one coarse
        # clock tick visible, which the timestamp alone would miss. Mode makes a
        # ``chmod`` visible: flipping the executable bit is worktree activity
        # Git records, yet it moves no timestamp, size or inode.
        term = hash(
            (
                path,
                stat_result.st_mtime_ns,
                stat_result.st_size,
                stat_result.st_ino,
                stat_result.st_mode,
            ),
        )
    return newest, (fingerprint + term) & _FINGERPRINT_MASK


def _entry_stat(entry: os.DirEntry[str]) -> os.stat_result:
    """Stat one walked entry, letting ``OSError`` mark the scan incomplete.

    Suppressing the error here would fold the entry into the fingerprint as a
    stable ``(path, None)`` term and, for a directory, stop the walk from
    descending into it — a scan claiming completeness while blind to every
    later write under that path. The caller turns the error into ``None``
    ("could not tell") instead.
    """
    return entry.stat(follow_symlinks=False)


def _stat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except OSError:
        return None


def _resolve_linked_git_dir(worktree_path: Path) -> Path | None:
    """Resolve a ``gitdir:`` pointer file to the real git dir, if present.

    A plain ``.git`` *directory* needs no special handling — the walk already
    covers it — so only the linked-worktree pointer file resolves here.
    """
    git_path = worktree_path / ".git"
    try:
        if git_path.is_dir():
            return None
        content = git_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith(_GITDIR_PREFIX):
            continue
        raw = stripped[len(_GITDIR_PREFIX) :].strip()
        if not raw:
            return None
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else worktree_path / candidate
    return None
