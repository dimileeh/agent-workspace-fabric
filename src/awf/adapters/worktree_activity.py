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
  entry's path, mtime, size and inode, combined order-independently — against
  the previous probe's, not by tracking one newest mtime. A single maximum
  timestamp is blinded by a single future-dated entry: the first probe would
  adopt that stamp as the floor, and every later write, stamped with the current
  clock, would land below it and read as idleness. That is one spurious
  extension followed by an idle kill of a run that is still working — the #932
  defect again. A fingerprint also has no skew against the kernel's coarse inode
  clock, and a write that races the walk is simply reported by the next probe
  instead of being swallowed.
* Only the *first* probe has nothing to compare against, so it alone is
  clock-based: it asks whether anything is newer than a seed taken when the
  probe was built, with a small tolerance for that coarse-clock lag.
* The walk is bounded by an entry budget, and running out fails **open**: the
  probe reports ``None`` ("could not tell"), which the watchdog counts as
  activity. A truncated walk has no opinion about liveness, and any worktree
  with a ``node_modules`` / ``.venv`` in it exhausts the budget on every probe,
  so failing closed there would idle-kill every healthy run in such a
  repository — the #932 defect again. The wall timeout remains the hard cap.
"""

from __future__ import annotations

import asyncio
import os
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
        """
        scan = await asyncio.to_thread(self._scan)
        if scan is None:
            return None
        previous, self._previous = self._previous, scan
        if previous is None:
            # Nothing observed yet, so the construction-time seed is the only
            # reference point this one probe has.
            return scan.newest_mtime > self._seed
        return scan.fingerprint != previous.fingerprint

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
                        # bumps the containing directory.
                        newest, fingerprint = _absorb(
                            newest,
                            fingerprint,
                            entry.path,
                            _entry_stat_or_none(entry),
                        )
                        if _entry_is_directory(entry):
                            stack.append(entry.path)
            except OSError:
                # An unreadable directory is skipped, not fatal — the rest of
                # the tree still reports liveness.
                continue
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
        # clock tick visible, which the timestamp alone would miss.
        term = hash(
            (path, stat_result.st_mtime_ns, stat_result.st_size, stat_result.st_ino),
        )
    return newest, (fingerprint + term) & _FINGERPRINT_MASK


def _entry_stat_or_none(entry: os.DirEntry[str]) -> os.stat_result | None:
    try:
        return entry.stat(follow_symlinks=False)
    except OSError:
        return None


def _entry_is_directory(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


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
