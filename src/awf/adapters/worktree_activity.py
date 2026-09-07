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
  entry's path, mtime, ctime, size, inode and mode, combined order-independently — against
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
* A baseline fingerprint is taken by :func:`make_worktree_activity_probe`
  before the agent is started, so even the first probe compares fingerprints.
  Mode changes are why that matters: ``chmod +x`` moves no mtime, so if the
  agent's only activity during the first idle window is a mode change, a
  clock-based first probe answers "idle" and the confirming rescan then
  compares two identical post-``chmod`` scans — an idle kill of a run that was
  working.
* Every scan runs in this module's **own** thread pool, not the process-wide
  default executor ``asyncio.to_thread`` uses. A stalled filesystem call cannot
  be cancelled, so the timeouts below only abandon the *wait* — the thread runs
  on. Sharing the default executor would let those abandoned threads consume the
  workers the rest of the control plane's ``to_thread`` work needs; isolated,
  they can only slow other worktree scans, which fail open.
* Priming is best-effort, and *bounded*. It runs before the agent starts, so it
  is outside the run's wall budget: an unbounded wait on a stalled ``scandir``
  would wedge the worker with no timeout to escape through, and an unexpected
  error would abort a run that had not started instead of starting it without a
  baseline. Priming therefore has its own cap and fails open on any error. A
  truncated — or capped, or failed — priming walk leaves nothing to compare
  against, and only then is the first probe clock-based: it asks whether
  anything is newer than a seed taken when the probe was built, with a small
  tolerance for that coarse-clock lag. A clock is blind to a change that moves
  no mtime, so that probe can answer "activity" or "could not tell" — never
  "idle". Answering idleness there would resurrect the very ``chmod`` kill
  priming exists to prevent, in the one window where priming failed. It costs at
  most one idle window per run: the complete scan it just took is the baseline
  every later probe compares fingerprints against.
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
  descended into, so nothing under it is ever observed. The linked worktree's
  external ``HEAD`` / ``index`` / ``logs/HEAD`` are the same: they are the only
  place Git-only activity shows up, so one of them being unreadable is an
  incomplete scan too — only their *absence* is a complete observation. So is an
  unreadable ``.git`` pointer file, which would otherwise be indistinguishable
  from a plain checkout and drop those three paths from an otherwise
  complete-looking fingerprint. Any incomplete observation is ``None``, not
  ``False``.
"""

from __future__ import annotations

import asyncio
import os
import stat as stat_module
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from awf.common.commands import ActivityProbe
from awf.common.logging import get_logger

_log = get_logger(__name__)

# Bounded so one probe can never walk an unbounded tree on the worker.
DEFAULT_MAX_ENTRIES = 200_000

# Priming runs before the agent starts, so the run's wall timeout is not yet
# holding anything back. Generous enough for a cold walk of a large worktree,
# but finite: a stalled ``scandir`` / ``stat`` must not park the worker forever.
DEFAULT_PRIME_TIMEOUT_SECONDS = 120.0

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

# Scans run in a pool of their own, never the interpreter-wide default executor
# behind ``asyncio.to_thread``. A stalled ``scandir`` / ``stat`` cannot be
# interrupted, and every caller here abandons the wait under a timeout, so the
# thread keeps running until the filesystem answers. In the shared executor those
# abandoned threads pile up against the one fixed worker count every other
# ``asyncio.to_thread`` caller in this process draws from — git, Docker and GC
# work would end up queued behind a wedged worktree whose own agent timeouts have
# already given up on it. Isolated, the blast radius of a stalled filesystem is
# other worktree scans, and those fail open: a scan that cannot get a worker in
# time is abandoned as "could not tell", which the watchdog counts as activity.
# Sized like the default executor, so healthy probes are no more serialised than
# they were before.
_SCAN_THREAD_NAME_PREFIX = "awf-worktree-scan"
_SCAN_EXECUTOR_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)
_scan_executor_lock = threading.Lock()
_scan_executor: ThreadPoolExecutor | None = None


def _get_scan_executor() -> ThreadPoolExecutor:
    """The probe's own worker pool, created on first use and kept for the process.

    Lazily built so a control plane that never probes pays no threads for it, and
    long-lived because the alternative — an executor per probe — would let a
    stalled scan leak a whole pool per workspace instead of sharing one bound.
    """
    global _scan_executor
    with _scan_executor_lock:
        if _scan_executor is None:
            _scan_executor = ThreadPoolExecutor(
                max_workers=_SCAN_EXECUTOR_MAX_WORKERS,
                thread_name_prefix=_SCAN_THREAD_NAME_PREFIX,
            )
        return _scan_executor


async def _run_scan[ScanResultT](work: Callable[[], ScanResultT]) -> ScanResultT:
    """Run one blocking scan off the event loop, in the isolated pool above."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_scan_executor(), work)


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
        prime_timeout_seconds: float = DEFAULT_PRIME_TIMEOUT_SECONDS,
    ) -> None:
        self._worktree_path = worktree_path
        self._max_entries = max_entries
        self._prime_timeout_seconds = prime_timeout_seconds
        self._previous: _Scan | None = None
        # Wall clock, because ``st_mtime`` is wall clock, and only ever read by
        # a first probe that priming left without a baseline. Never compared
        # against the event loop's monotonic clock — the probe only returns a
        # boolean.
        self._seed = time.time() - _COARSE_CLOCK_TOLERANCE_SECONDS

    async def prime(self) -> bool:
        """Record the pre-run baseline, so the first probe compares fingerprints.

        Returns whether the worktree is worth watching at all: ``False`` only
        when the bounded check below positively found nothing there.

        Called before the agent is started. Without it the first probe has only
        the construction-time clock seed, which is blind to any change that
        moves no mtime — a ``chmod`` is worktree activity Git records, and the
        confirming rescan cannot see it either, because by then both scans are
        post-change. A truncated walk simply leaves no baseline and the seed
        stays in charge; priming never makes the probe *less* informed.

        Which is why it also fails **open**, under its own cap. This runs before
        the agent is started, outside the run's wall timeout, and the scan sits
        in an isolated-pool thread that cannot be interrupted from here — so an
        unbounded await
        on a stalled ``scandir`` / ``stat`` (or on ``.git`` being a pointer to
        somewhere that blocks) would wedge the worker with no deadline to escape
        through. Abandoning the wait leaves the thread to finish on its own. Any
        error is likewise only a missing baseline: letting it escape would abort
        a run that has not started over a best-effort optimisation. Both land in
        the documented degraded mode — no baseline, seed in charge for one probe
        — which is strictly better than not running the agent at all, so an
        existence check that stalls or raises keeps the probe rather than
        dropping the watchdog back to the stdout-only cap #932 is about.
        ``CancelledError`` is a ``BaseException`` and is deliberately not
        absorbed.
        """
        try:
            present, baseline = await asyncio.wait_for(
                _run_scan(self._prime_scan),
                timeout=self._prime_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - priming is best-effort, see above.
            _log.warning(
                "agent.worktree_activity.prime_failed",
                worktree_path=str(self._worktree_path),
                prime_timeout_seconds=self._prime_timeout_seconds,
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            self._previous = None
            return True
        self._previous = baseline
        return present

    def _prime_scan(self) -> tuple[bool, _Scan | None]:
        """Check the worktree is there and walk it, both inside the bounded thread.

        ``Path.exists`` is a ``stat`` on the same filesystem the walk is about to
        traverse, so it stalls in exactly the cases the cap above exists for. Run
        on the event loop it would block the worker — the agent never starts, and
        neither the priming timeout nor the run's wall deadline is reachable to
        recover it — so it belongs on this side of ``wait_for`` with the walk.
        """
        if not self._worktree_path.exists():
            return False, None
        return True, self._scan()

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
        scan = await _run_scan(self._scan)
        if scan is None:
            return None
        previous, self._previous = self._previous, scan
        if previous is None:
            return self._first_probe_answer(scan)
        if scan.fingerprint != previous.fingerprint:
            return True
        return await self._confirm_idle(scan)

    def _first_probe_answer(self, scan: _Scan) -> bool | None:
        """Answer without a baseline: activity, or "could not tell" — never idle.

        Priming was truncated, so the construction-time seed is the only
        reference point this one probe has, and a clock only sees changes that
        move an mtime. A ``chmod`` moves none, and the confirming rescan cannot
        help — by then both scans are post-change — so "nothing newer than the
        seed" is not evidence of idleness here. Fail open exactly like a
        truncated walk: the scan just taken becomes the baseline, so the next
        probe compares fingerprints like every other one.
        """
        if scan.newest_mtime > self._seed:
            return True
        return None

    async def _confirm_idle(self, scan: _Scan) -> bool | None:
        """Rescan, because a write can race a walk without changing its result.

        Modifying an existing regular file leaves its parent directory's mtime
        alone, so a write landing after the walk stat-ed that entry is invisible
        to the scan it raced. A negative answer is final — the watchdog fires the
        idle timeout on it rather than probing again — so the scan is repeated
        before "nothing moved" is believed. The rescan begins after the raced
        walk finished and therefore stats that entry after the write.
        """
        confirm = await _run_scan(self._scan)
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
        try:
            git_dir_paths = self._git_dir_paths()
        except OSError as exc:
            # The ``.git`` pointer is there but unreadable from here — the
            # control plane and the agent run as different users. Falling back
            # to "not a linked worktree" would drop HEAD / index / logs/HEAD
            # from the scan while still returning a fingerprint that claims to
            # be complete, so Git-only activity would read as idleness. Same
            # fail-open rule as any other incomplete observation.
            _log.warning(
                "agent.worktree_activity.git_pointer_unreadable",
                worktree_path=str(self._worktree_path),
                path=str(self._worktree_path / ".git"),
                error=str(exc),
            )
            return None
        for path in (self._worktree_path, *git_dir_paths):
            try:
                stat_result = _metadata_stat(path)
            except OSError as exc:
                # Same fail-open rule as an unstattable walked entry: the linked
                # worktree's HEAD / index / logs/HEAD are the only place
                # Git-only activity shows up, so folding an unreadable one in as
                # a stable term would claim a complete scan while being blind to
                # every commit and index write the agent still makes there.
                _log.warning(
                    "agent.worktree_activity.metadata_unreadable",
                    worktree_path=str(self._worktree_path),
                    path=str(path),
                    error=str(exc),
                )
                return None
            newest, fingerprint = _absorb(newest, fingerprint, str(path), stat_result)
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


async def make_worktree_activity_probe(worktree_path: Path | None) -> ActivityProbe | None:
    """Build a primed probe for ``worktree_path``, or ``None`` with nothing to watch.

    Priming walks the tree once, so this must be awaited before the agent is
    started: the baseline it records is only a pre-run one if nothing the agent
    does can land ahead of it. "Is there anything to watch?" is answered inside
    that same bounded operation, because it is one more ``stat`` on a filesystem
    that may be stalled and the event loop is the one place no deadline can
    reach it.
    """
    if worktree_path is None:
        return None
    probe = WorktreeActivityProbe(worktree_path)
    if not await probe.prime():
        return None
    return probe


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
        # Git records, yet it moves no timestamp, size or inode. Change time
        # covers the rest: a same-length in-place rewrite whose mtime is then
        # restored (a timestamp-preserving formatter, ``rsync --inplace
        # --times``) moves nothing else in this tuple, and neither does a
        # ``chown`` or an xattr-only edit. ``st_ctime_ns`` moves for all of
        # them and a process cannot set it back.
        term = hash(
            (
                path,
                stat_result.st_mtime_ns,
                stat_result.st_ctime_ns,
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


def _metadata_stat(path: Path) -> os.stat_result | None:
    """Stat a watched metadata path; ``None`` only when it is positively absent.

    ``logs/HEAD`` exists only once a reflog does, so "not there" has to stay a
    complete observation folded in as a stable ``(path, None)`` term — otherwise
    the probe would answer "could not tell" forever and the watchdog would never
    fire. Every other error is the opposite: the path may be moving without this
    process being able to see it, so the ``OSError`` propagates and the caller
    marks the scan incomplete.
    """
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _resolve_linked_git_dir(worktree_path: Path) -> Path | None:
    """Resolve a ``gitdir:`` pointer file to the real git dir, if present.

    A plain ``.git`` *directory* needs no special handling — the walk already
    covers it — so only the linked-worktree pointer file resolves here.

    ``None`` means "nothing external to watch", which has to stay a *complete*
    observation: no ``.git`` at all, a plain directory, or a pointer file that
    names no usable git dir. A pointer that exists but cannot be read is not
    that — the git dir it names may be moving without this process being able
    to see it — so the ``OSError`` propagates and the caller marks the scan
    incomplete rather than silently scanning as if the checkout were not
    linked.
    """
    git_path = worktree_path / ".git"
    try:
        if git_path.is_dir():
            return None
        content = git_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
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
