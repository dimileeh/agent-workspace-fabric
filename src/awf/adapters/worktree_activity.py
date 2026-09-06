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
* The baseline is the **newest mtime actually observed**, not a wall clock
  reading. Linux stamps inodes from a coarse clock that can lag
  ``time.time()`` by a timer tick, so a clock-based baseline silently loses
  writes made just after it was taken. Comparing observed mtimes against each
  other has no such skew, and a write that races the walk is simply reported by
  the next probe instead of being swallowed. Only the *initial* baseline is
  clock-based (there is nothing observed yet); it carries a small tolerance for
  exactly that coarse-clock lag.
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

from awf.common.commands import ActivityProbe
from awf.common.logging import get_logger

_log = get_logger(__name__)

# Bounded so one probe can never walk an unbounded tree on the worker.
DEFAULT_MAX_ENTRIES = 200_000

# Slack for the kernel's coarse inode-timestamp clock lagging ``time.time()``.
# Only ever applied to the seed baseline; at worst it grants one extra idle
# window to a run whose worktree was touched moments before it started.
_COARSE_CLOCK_TOLERANCE_SECONDS = 0.05

_GITDIR_PREFIX = "gitdir:"
# Files that move when only Git state changed in a linked worktree.
_GIT_DIR_ACTIVITY_FILES = (Path("HEAD"), Path("index"), Path("logs") / "HEAD")


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
        # Wall clock, because ``st_mtime`` is wall clock. Never compared against
        # the event loop's monotonic clock — the probe only returns a boolean.
        self._baseline = time.time() - _COARSE_CLOCK_TOLERANCE_SECONDS

    async def __call__(self) -> bool | None:
        """Scan off the event loop and advance the baseline to what it saw.

        Returns ``None`` when the walk was truncated: the scan saw part of the
        tree and cannot claim the worktree was idle. The baseline is left alone
        in that case, so a later complete scan still reports the change it
        missed.
        """
        baseline = self._baseline
        newest = await asyncio.to_thread(self._newest_mtime)
        if newest is None:
            return None
        if newest <= baseline:
            return False
        self._baseline = newest
        return True

    def _newest_mtime(self) -> float | None:
        """Newest mtime under the worktree, or ``None`` if the walk was truncated."""
        newest = _newest_of(
            (_mtime_or_none(self._worktree_path), *self._git_dir_mtimes()),
        )
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
                        # Directory mtimes count too: a create / delete / rename
                        # only bumps the containing directory.
                        newest = _newest_of((newest, _entry_mtime_or_none(entry)))
                        if _entry_is_directory(entry):
                            stack.append(entry.path)
            except OSError:
                # An unreadable directory is skipped, not fatal — the rest of
                # the tree still reports liveness.
                continue
        return newest

    def _git_dir_mtimes(self) -> tuple[float | None, ...]:
        git_dir = _resolve_linked_git_dir(self._worktree_path)
        if git_dir is None:
            return ()
        return tuple(_mtime_or_none(git_dir / name) for name in _GIT_DIR_ACTIVITY_FILES)


def make_worktree_activity_probe(worktree_path: Path | None) -> ActivityProbe | None:
    """Build a probe for ``worktree_path``, or ``None`` when there is nothing to watch."""
    if worktree_path is None or not worktree_path.exists():
        return None
    return WorktreeActivityProbe(worktree_path)


def _newest_of(values: tuple[float | None, ...]) -> float:
    return max((value for value in values if value is not None), default=0.0)


def _entry_mtime_or_none(entry: os.DirEntry[str]) -> float | None:
    try:
        return entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return None


def _entry_is_directory(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _mtime_or_none(path: Path) -> float | None:
    try:
        return path.lstat().st_mtime
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
