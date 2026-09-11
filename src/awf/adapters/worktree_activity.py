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
  worktree, ``.git`` included. Each scan opens the worktree root without
  following symlinks, then opens observed descendants descriptor-relative to
  their already-open parents. A directory renamed after its lstat therefore
  cannot redirect a queued scan through a replacement symlink.
* No marker file is written. A marker inside the worktree would show up as dirty
  residue in the verdict / dirty-sink fingerprints, and shelling out to
  ``find(1)`` would add a dependency on the control-plane image's toolchain.
* A *linked* worktree keeps HEAD/index outside the tree (``.git`` is a
  ``gitdir:`` pointer file), so "the index or HEAD moved" is only observable by
  also stat-ing the resolved git dir's ``HEAD`` / ``index`` / ``logs/HEAD`` —
  and the branch ref HEAD names, which lives under the git dir's ``commondir``
  and is the one path a files-backend commit always writes. The other three can
  all sit still through one: HEAD keeps naming the same branch, ``logs/HEAD`` is
  not written when ``core.logAllRefUpdates`` is off, and an index already
  matching the previous probe is not rewritten. Under the ``reftable`` backend
  the branch has no loose ref file to land in either, so the worktree's *own*
  ``reftable/tables.list`` — where the refs and reflogs belonging to this
  worktree alone, HEAD's among them, are rewritten by every ref transaction it
  makes — is watched too. The git dir and its ``commondir`` are resolved and
  pinned by the bounded pre-agent prime scan; later scans never accept a
  replacement pointer or symlink target selected by the agent. Its device/inode
  identity is pinned too; the common dir is pinned the same way, and later
  scans resolve every watched descendant descriptor-relative without following
  symlinks. So is ``FETCH_HEAD``,
  the one path in the worktree's own Git state a quiet ``git fetch`` is
  guaranteed to move. A linked worktree's git dir holds nothing *but* this
  worktree's state, so it is **walked whole** like a second tree root rather
  than reduced to a list of names: that is what covers the metadata no name
  above reaches — an in-place rewrite of ``ORIG_HEAD`` or ``COMMIT_EDITMSG``,
  and every step a rebase or a cherry-pick sequence writes inside an already
  existing ``rebase-merge`` / ``sequencer`` directory. The shared common dir is
  deliberately not walked, and nothing shared under it is watched by name
  either: one bare mirror backs every worktree of a repo, so its churn is other
  workspaces' agents and would report this one as alive regardless. That
  includes the common ``reftable/tables.list``, which every worktree's ref
  transactions rewrite. Only the one path under it that belongs to this
  worktree — its branch ref — is watched by name.
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
* Every scan runs on a **daemon thread of this module's own**, not on the
  process-wide default executor ``asyncio.to_thread`` uses and not in a
  ``ThreadPoolExecutor``. A stalled filesystem call cannot be cancelled, so the
  timeouts below only abandon the *wait* — the thread runs on. Sharing the
  default executor would let those abandoned threads consume the workers the rest
  of the control plane's ``to_thread`` work needs; any executor at all would also
  hold up interpreter shutdown, because ``concurrent.futures`` joins every worker
  on the way out and a worker parked on a stalled ``scandir`` never returns. AWF
  owns worker lifecycle, so a restart must not hang behind a scan the agent's own
  timeout already gave up on. A daemon thread is joined by nobody.
* Because nobody joins them, each probe admits **one scan thread at a time**. An
  abandoned scan is left running, the probe fails open, and the watchdog reads
  that as activity — so a worktree whose filesystem never answers looks like a
  busy agent and would shed another unkillable thread every idle window, without
  end when the run has no wall timeout. Enough of those exhaust the worker's
  thread/PID limits and take unrelated workspaces down with them. So a probe
  whose previous scan is still running does not start a successor: it answers
  "could not tell" immediately, which is what the stalled scan meant anyway.
  That bound is per worktree, and the worker runs many, so all of them also
  draw from a process-wide ceiling of live scan threads: a scan with no slot
  left starts no thread and fails open the same way, which keeps the worst case
  at a fixed number of unreclaimable threads however many worktrees wedge.
* Priming is best-effort, and *bounded*. It runs before the agent starts, so it
  is outside the run's wall budget: an unbounded wait on a stalled ``scandir``
  would wedge the worker with no timeout to escape through, and an unexpected
  error would abort a run that had not started instead of starting it without a
  baseline. Priming therefore has its own cap and fails open on any error. A
  truncated — or capped, or failed — priming walk leaves nothing to compare
  against. Whenever a safe follow-up scan is possible, the first probe is then
  clock-based: it asks whether
  anything is newer than a seed taken when the probe was built, with a small
  tolerance for that coarse-clock lag. A clock is blind to a change that moves
  no mtime, so that probe can answer "activity" or "could not tell" — never
  "idle". Answering idleness there would resurrect the very ``chmod`` kill
  priming exists to prevent, in the one window where priming failed. It costs at
  most one idle window per run: the complete scan it just took is the baseline
  every later probe compares fingerprints against. If priming failed before it
  could pin external Git roots, later scans resolve nothing agent-controlled:
  an absent or real-directory ``.git`` remains covered by the worktree walk,
  while a pointer or symlink fails open for the rest of the run.
* Managed local worktrees additionally receive their expected Git admin and
  common directories from ``GitManager``. Those paths are derived from the
  workspace id and repository URL rather than from the agent-writable ``.git``
  marker, so a later adapter invocation cannot adopt a pointer or symlink an
  earlier invocation redirected elsewhere. The marker is checked without
  following it, and the private admin directory's ``commondir`` file must still
  select GitManager's common directory during priming and every later scan. A
  mismatch leaves the probe in fail-open degraded mode and no agent-selected
  external root is pinned or walked.
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
import errno
import itertools
import os
import stat as stat_module
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any, NamedTuple

from awf.common.commands import ActivityProbe
from awf.common.logging import get_logger

_log = get_logger(__name__)

# Bounded so one probe can never walk an unbounded tree on the worker.
DEFAULT_MAX_ENTRIES = 200_000

# Priming runs before the agent starts, so the run's wall timeout is not yet
# holding anything back. Generous enough for a cold walk of a large worktree,
# but finite: a stalled ``scandir`` / ``stat`` must not park the worker forever.
DEFAULT_PRIME_TIMEOUT_SECONDS = 120.0

# The process-wide ceiling on scan threads that are still running. The per-probe
# gate below bounds one worktree to one live scan; this bounds the worker as a
# whole, because the gates know nothing about each other and enough wedged
# worktrees would otherwise park one unreclaimable thread each. Far above what
# a healthy worker needs — one live scan per concurrently running agent — and
# far below the thread/PID limits whose exhaustion would stop unrelated agents
# from starting at all.
DEFAULT_MAX_LIVE_SCAN_THREADS = 64

# Slack for the kernel's coarse inode-timestamp clock lagging ``time.time()``.
# Only ever applied to the seed, which only the first probe consults; at worst
# it grants one extra idle window to a run whose worktree was touched moments
# before it started.
_COARSE_CLOCK_TOLERANCE_SECONDS = 0.05

# Fingerprint terms are summed, so the walk order cannot change the result.
_FINGERPRINT_MASK = (1 << 64) - 1

_GITDIR_PREFIX = "gitdir:"
_GITFILE_MAX_BYTES = 4096
_HEAD_REF_PREFIX = "ref:"
_GIT_COMMON_DIR_FILE = "commondir"
# Per-worktree files that move when only Git state changed in a linked worktree.
# The branch ref a commit actually lands in lives under the *common* git dir and
# is resolved per scan, from HEAD, by ``_resolve_head_branch_ref`` below.
# ``FETCH_HEAD`` is per-worktree too and every ``git fetch`` rewrites it: a quiet
# fetch touches nothing in the worktree, and its objects and remote-tracking refs
# land in the shared common dir, so this is the one path in the worktree's own
# Git state such a fetch is guaranteed to move.
_GIT_DIR_ACTIVITY_FILES = (
    Path("HEAD"),
    Path("index"),
    Path("logs") / "HEAD",
    Path("FETCH_HEAD"),
)
# The stack file of the ``reftable`` backend (``extensions.refStorage=reftable``),
# rewritten by every ref transaction. Under that backend there is no loose ref
# file for the resolved branch to land in, and ``HEAD`` is a stub naming
# ``refs/heads/.invalid``, so this is where a commit here shows up instead.
# Watched under the worktree's own git dir only — that stack holds the refs and
# reflogs that are this worktree's alone, HEAD's among them. The common dir's
# stack is shared by every worktree of the repo, so its churn is a neighbouring
# workspace's and is deliberately left out; see ``_git_dir_paths``.
_REFTABLE_STACK_FILE = Path("reftable") / "tables.list"

# Each scan gets a daemon thread of its own, never the interpreter-wide default
# executor behind ``asyncio.to_thread`` and never a pool. A stalled ``scandir`` /
# ``stat`` cannot be interrupted, and every caller here abandons the wait under a
# timeout, so the thread keeps running until the filesystem answers.
#
# In the shared executor those abandoned threads pile up against the one fixed
# worker count every other ``asyncio.to_thread`` caller in this process draws
# from — git, Docker and GC work would end up queued behind a wedged worktree
# whose own agent timeouts have already given up on it.
#
# A private ``ThreadPoolExecutor`` fixes that but not the worse half: executor
# workers are non-daemon threads registered with ``concurrent.futures``' exit
# hook, which joins every one of them while the interpreter shuts down. One
# abandoned scan on a stalled filesystem would therefore hang a graceful worker
# restart indefinitely — the control plane cannot own lifecycle through a
# shutdown it cannot complete. Daemon threads participate in no such join: the
# interpreter leaves them where they are.
#
# Unpooled is also what keeps the blast radius at "other worktree scans" rather
# than "no worktree scans": a fixed shared pool that stalled scans have filled
# makes healthy probes on healthy *other* worktrees wait for a worker.
#
# Unbounded is not the alternative, though. Failing open is what makes
# abandonment quiet: a worktree whose filesystem never answers looks exactly like
# a busy agent, one "activity" reply per idle window, while a thread nothing can
# reclaim is left behind each time — every idle window for the rest of a run
# that, without a wall timeout, has no end. Enough wedged worktrees would
# exhaust the worker's thread/PID or address-space limits and stop unrelated
# agents from running at all. So every scan is registered with the ``_ScanGate``
# below, and a probe whose previous scan is still running starts no successor:
# at most one live scan thread per worktree, and therefore at most one per
# running agent. Skipping costs nothing an unanswerable scan was going to
# provide — both are "could not tell", which the watchdog reads as activity.
#
# One gate only knows about its own worktree, though, and the worker runs many:
# N wedged workspaces still park N threads nothing can reclaim, and a run that
# retries its agent builds a fresh probe — and therefore a fresh gate — each
# time. So the gates share a process-wide ceiling as well. A scan with no slot
# left starts no thread at all and fails open exactly like a gated one; the
# alternative is ``threading.Thread.start`` raising once the interpreter is
# already out of threads, in a worker whose git, Docker and API work needs
# threads of its own.
#
# Each abandoned wait is warned about besides, naming the worktree, because a
# gated probe is otherwise indistinguishable from a healthy one and an operator
# has to be able to see which workspace stopped answering.
_SCAN_THREAD_NAME_PREFIX = "awf-worktree-scan"
_scan_sequence = itertools.count()


class _ScanCapacityError(RuntimeError):
    """No process-wide slot left for another thread nothing could reclaim."""


# Public name for that exit: ``probe_worktree_filesystem`` below lends this
# module's thread mechanism to other worktree probes, and they have to be able to
# name what "no slot left" raises.
WorktreeProbeCapacityError = _ScanCapacityError


class _LiveScanThreads:
    """Process-wide count of scan threads that have not finished yet.

    Counted rather than pooled: a slot is held by a *running* thread, including
    one whose caller has long since given up, and is returned only when the
    filesystem finally answers it. That is the quantity the worker's thread/PID
    limits care about.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._live = 0

    @property
    def limit(self) -> int:
        return self._limit

    def acquire(self) -> bool:
        """Take a slot for a scan about to start, or report there is none."""
        with self._lock:
            if self._live >= self._limit:
                return False
            self._live += 1
            return True

    def release(self) -> None:
        """Return the slot of a scan thread that has finished."""
        with self._lock:
            self._live -= 1


_live_scan_threads = _LiveScanThreads(DEFAULT_MAX_LIVE_SCAN_THREADS)


class _ScanGate:
    """The one scan thread a worktree is allowed to have in flight.

    A scan is only ever abandoned, never cancelled, so the future the thread
    publishes into outlives the caller that gave up on it and settles when the
    filesystem finally answers. Holding it is therefore an accurate answer to
    "is that thread still out there?", and the only bound available on threads
    nothing can reclaim.
    """

    def __init__(self) -> None:
        self._outstanding: Future[Any] | None = None

    def is_busy(self) -> bool:
        """True while a previously started scan's thread is still running."""
        outstanding = self._outstanding
        if outstanding is None:
            return False
        if not outstanding.done():
            return True
        # Settled — the thread is gone (or was never started, the caller having
        # cancelled it first). Drop the reference so it is not held for the life
        # of the probe.
        self._outstanding = None
        return False

    def hold(self, scan: Future[Any]) -> None:
        """Remember the scan just started as this worktree's in-flight one."""
        self._outstanding = scan


async def _run_scan[ScanResultT](
    work: Callable[[], ScanResultT],
    *,
    worktree_path: str,
    gate: _ScanGate,
) -> ScanResultT:
    """Run one blocking scan off the event loop, on an abandonable daemon thread.

    Raises :class:`_ScanCapacityError` when the worker already holds as many
    unfinished scan threads as it is allowed; every caller turns that into the
    same "could not tell" a stalled scan would have produced.
    """
    slots = _live_scan_threads
    if not slots.acquire():
        _log.warning(
            "agent.worktree_activity.scan_capacity_exhausted",
            worktree_path=worktree_path,
            max_live_scan_threads=slots.limit,
        )
        raise _ScanCapacityError(
            f"{slots.limit} worktree scan threads are still running",
        )
    result: Future[ScanResultT] = Future()
    gate.hold(result)

    def _deliver() -> None:
        try:
            if not result.set_running_or_notify_cancel():
                # The caller gave up before this thread was scheduled; nothing
                # to do beyond handing the slot back below.
                return
            try:
                result.set_result(work())
            except BaseException as exc:  # noqa: BLE001 - relayed to the awaiting caller.
                result.set_exception(exc)
        finally:
            # This thread is done, so the slot it held is free — whether the
            # caller is still waiting or abandoned it hours ago.
            slots.release()

    try:
        _start_scan_thread(_deliver)
    except RuntimeError as exc:
        # The interpreter is out of threads despite the ceiling above. Nothing
        # will run ``_deliver``, so the slot and the gate have to be released
        # here or this worktree would never be scanned again.
        slots.release()
        result.cancel()
        raise _ScanCapacityError(str(exc)) from exc
    # ``wrap_future`` bridges the thread's result back onto this loop and drops
    # it if the awaiting caller is already gone, exactly as ``run_in_executor``
    # did — only the worker underneath it changed.
    try:
        return await asyncio.wrap_future(result)
    except asyncio.CancelledError:
        if result.running():
            # The wait is over; the walk is not, and nothing can interrupt it.
            # Every caller turns this into "could not tell", which the watchdog
            # reads as activity — indistinguishable from a healthy run unless
            # the abandonment itself is on the record.
            _log.warning(
                "agent.worktree_activity.scan_abandoned",
                worktree_path=worktree_path,
            )
        raise


async def probe_worktree_filesystem[ProbeResultT](
    work: Callable[[], ProbeResultT],
    *,
    worktree_path: str,
) -> ProbeResultT:
    """Run one blocking worktree filesystem call on an abandonable daemon thread.

    The scans above are not the only probe the control plane runs over a worktree
    a timed-out agent was last touching, and every such caller needs what the
    comment above spells out: a probe its own timeout abandoned must not hold a
    worker the rest of the process's ``asyncio.to_thread`` work draws from, and
    must not hold a graceful restart up behind ``concurrent.futures``' exit join
    (PRRT_kwDOSJAM6s6f5q9F). Callers bound their own wait; this supplies the
    thread and the process-wide ceiling, raising :class:`WorktreeProbeCapacityError`
    when the worker already holds as many unfinished probe threads as it allows.

    A one-shot probe carries no gate across calls — a gate bounds a *repeating*
    probe's successors, and there are none here — so the shared ceiling is the
    whole bound it draws on.
    """
    return await _run_scan(work, worktree_path=worktree_path, gate=_ScanGate())


def _start_scan_thread(deliver: Callable[[], None]) -> None:
    """Start one scan on a fresh daemon thread nothing will ever join."""
    threading.Thread(
        target=deliver,
        name=f"{_SCAN_THREAD_NAME_PREFIX}-{next(_scan_sequence)}",
        daemon=True,
    ).start()


class _Scan(NamedTuple):
    """One complete observation of the worktree."""

    newest_mtime: float
    fingerprint: int


class _DirectoryIdentity(NamedTuple):
    """A directory object captured before the agent can replace its path."""

    device: int
    inode: int


class _PinnedDirectory(NamedTuple):
    """A directory path and its observed identity, if it existed."""

    path: Path
    identity: _DirectoryIdentity | None
    anchor: _PinnedDirectory | None = None
    relative_to_anchor: Path | None = None


class _WatchedPath(NamedTuple):
    """One metadata path resolved beneath a pre-agent pinned directory."""

    root: _PinnedDirectory
    relative: Path

    @property
    def path(self) -> Path:
        """Absolute display/fingerprint path without using it for access."""
        return self.root.path / self.relative


class _GitPaths(NamedTuple):
    """Git state a scan must fold in from outside the worktree.

    ``watched`` paths are stat-ed descriptor-relative to their pinned roots —
    shared ones among them, so only the names that belong to this worktree are
    ever listed. ``walk_roots`` are walked whole, like the worktree itself, and
    so may only ever hold state this worktree alone writes.
    """

    watched: tuple[_WatchedPath, ...]
    walk_roots: tuple[_PinnedDirectory, ...]


class _GitLayout(NamedTuple):
    """External Git roots trusted because priming resolved them pre-agent."""

    git_dir: _PinnedDirectory
    common_dir: _PinnedDirectory


class WorktreeActivityProbe:
    """Report whether anything under a worktree changed since the last probe."""

    def __init__(
        self,
        worktree_path: Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        prime_timeout_seconds: float = DEFAULT_PRIME_TIMEOUT_SECONDS,
        trusted_git_roots: tuple[Path, Path] | None = None,
    ) -> None:
        self._worktree_path = worktree_path
        self._max_entries = max_entries
        self._prime_timeout_seconds = prime_timeout_seconds
        self._trusted_git_roots = trusted_git_roots
        self._previous: _Scan | None = None
        # One live scan thread per worktree: an abandoned scan cannot be
        # reclaimed, so probing again while it runs would leak another.
        self._scan_gate = _ScanGate()
        # External Git roots are agent-writable inputs once execution starts.
        # Priming pins their pre-agent values; the lock prevents a priming
        # thread abandoned during resolution from publishing an agent-modified
        # target after the timeout has already let execution begin.
        self._git_layout_lock = threading.Lock()
        self._git_layout: _GitLayout | None = None
        self._git_layout_pinned = False
        self._git_layout_sealed = False
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
        on a daemon thread that cannot be interrupted from here — so an
        unbounded await
        on a stalled ``scandir`` / ``stat`` (or on ``.git`` being a pointer to
        somewhere that blocks) would wedge the worker with no deadline to escape
        through. Abandoning the wait leaves the thread to finish on its own, and
        gates the probes that follow until it does — one unreclaimable scan
        thread per worktree is the whole budget. Any error is likewise only a
        missing baseline: letting it escape would abort a run that has not
        started over a best-effort optimisation. When the external Git layout
        was already pinned, this lands in the documented seed-based degraded
        mode. When it was not, later scans only trust a fixed lstat proving
        ``.git`` is absent or a real directory; a pointer or symlink stays
        "could not tell." An existence check that stalls or raises still keeps
        the probe rather than dropping the watchdog back to the stdout-only cap
        #932 is about.
        ``CancelledError`` is a ``BaseException`` and is deliberately not
        absorbed.
        """
        try:
            present, baseline = await asyncio.wait_for(
                _run_scan(
                    self._prime_scan,
                    worktree_path=str(self._worktree_path),
                    gate=self._scan_gate,
                ),
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
        finally:
            # No external root first resolved after this point can be trusted:
            # the caller starts the agent as soon as ``prime`` returns.
            self._seal_git_layout()
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
        if not self._pin_current_git_layout():
            return True, None
        return True, self._scan()

    def _pin_current_git_layout(self) -> bool:
        """Pin the current external Git roots unless pre-agent capture is closed."""
        with self._git_layout_lock:
            if self._git_layout_pinned:
                return True
            if self._git_layout_sealed:
                return False
        layout = (
            _resolve_git_layout(self._worktree_path)
            if self._trusted_git_roots is None
            else _resolve_git_layout(
                self._worktree_path,
                trusted_git_roots=self._trusted_git_roots,
            )
        )
        return self._commit_git_layout(layout)

    def _commit_git_layout(self, layout: _GitLayout | None) -> bool:
        """Publish a resolved layout only if priming is still pre-agent."""
        with self._git_layout_lock:
            if self._git_layout_sealed:
                return False
            self._git_layout = layout
            self._git_layout_pinned = True
            return True

    def _seal_git_layout(self) -> None:
        """Forbid a late priming thread from trusting post-prime metadata."""
        with self._git_layout_lock:
            self._git_layout_sealed = True

    async def __call__(self) -> bool | None:
        """Scan off the event loop and compare against what the last probe saw.

        Returns ``None`` when the walk was truncated — or when a scan this probe
        already abandoned is still running: the scan saw part of the tree, or
        none of it, and either way cannot claim the worktree was idle. The
        remembered scan is left alone in that case, so a later complete scan
        still reports the change it missed.

        A scan that observed no change is not yet proof of idleness — a write
        can race the walk — so it is confirmed by a rescan before answering
        ``False``.
        """
        scan = await self._gated_scan()
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
        confirm = await self._gated_scan()
        if confirm is None:
            # Same fail-open rule as any truncated walk: no opinion, and the
            # complete scan stays the baseline for the next probe.
            return None
        self._previous = confirm
        return confirm.fingerprint != scan.fingerprint

    async def _gated_scan(self) -> _Scan | None:
        """Scan, unless a scan this probe gave up on is still running.

        Nothing can reclaim a thread parked on a stalled ``scandir`` / ``stat``,
        so starting another one every idle window would leak threads for as long
        as the run lasts — unbounded when the run has no wall timeout, and
        eventually fatal to unrelated agents sharing the worker's thread/PID
        limits. The successor would also learn nothing the abandoned scan had
        not already failed to learn: both answer "could not tell", which the
        watchdog counts as activity, so the run keeps its fail-open treatment
        either way and the wall deadline stays the hard cap.
        """
        if self._scan_gate.is_busy():
            _log.warning(
                "agent.worktree_activity.scan_still_running",
                worktree_path=str(self._worktree_path),
            )
            return None
        try:
            return await _run_scan(
                self._scan,
                worktree_path=str(self._worktree_path),
                gate=self._scan_gate,
            )
        except _ScanCapacityError:
            # Other worktrees hold every scan thread the worker allows. Same
            # answer as this probe's own gate: "could not tell", the remembered
            # scan untouched, and no thread added to the pile that caused it.
            return None

    def _scan(self) -> _Scan | None:
        """Fingerprint the worktree, or ``None`` if the walk was truncated."""
        newest = 0.0
        fingerprint = 0
        try:
            git_paths = self._git_dir_paths()
        except OSError as exc:
            # The ``.git`` pointer — or the HEAD / ``commondir`` naming the
            # branch ref behind it — is there but unreadable from here: the
            # control plane and the agent run as different users. Falling back
            # to "not a linked worktree", or to "no branch ref", would drop the
            # paths Git-only activity shows up in while still returning a
            # fingerprint that claims to be complete, so a commit would read as
            # idleness. Same fail-open rule as any other incomplete observation.
            _log.warning(
                "agent.worktree_activity.git_pointer_unreadable",
                worktree_path=str(self._worktree_path),
                path=str(exc.filename or self._worktree_path / ".git"),
                error=str(exc),
            )
            return None
        if git_paths is None:
            # Priming ended before its worker could establish the external Git
            # roots. Resolving them now would trust agent-controlled metadata;
            # omitting them would make a later Git-only write look idle.
            return None
        try:
            worktree_stat = _metadata_stat(self._worktree_path)
        except OSError as exc:
            _log.warning(
                "agent.worktree_activity.metadata_unreadable",
                worktree_path=str(self._worktree_path),
                path=str(self._worktree_path),
                error=str(exc),
            )
            return None
        newest, fingerprint = _absorb(
            newest,
            fingerprint,
            str(self._worktree_path),
            worktree_stat,
        )
        for watched in git_paths.watched:
            try:
                stat_result = _metadata_stat_at(watched)
            except OSError as exc:
                # Same fail-open rule as an unstattable walked entry: the linked
                # worktree's HEAD / index / logs/HEAD are the only place
                # Git-only activity shows up, so folding an unreadable one in as
                # a stable term would claim a complete scan while being blind to
                # every commit and index write the agent still makes there.
                _log.warning(
                    "agent.worktree_activity.metadata_unreadable",
                    worktree_path=str(self._worktree_path),
                    path=str(watched.path),
                    error=str(exc),
                )
                return None
            newest, fingerprint = _absorb(
                newest,
                fingerprint,
                str(watched.path),
                stat_result,
            )
        worktree_root = _PinnedDirectory(
            self._worktree_path,
            (
                _DirectoryIdentity(worktree_stat.st_dev, worktree_stat.st_ino)
                if worktree_stat is not None
                else None
            ),
        )
        try:
            worktree_descriptor = _open_pinned_directory(worktree_root)
        except OSError as exc:
            # Opening the root without following its final component keeps an
            # agent-controlled replacement symlink from redirecting the walk.
            _log.warning(
                "agent.worktree_activity.subtree_unreadable",
                worktree_path=str(self._worktree_path),
                path=str(self._worktree_path),
                error=str(exc),
            )
            return None
        stack = [(str(self._worktree_path), worktree_descriptor)]
        for root in git_paths.walk_roots:
            try:
                root_descriptor = _open_pinned_directory(root)
            except OSError as exc:
                # The lstat in ``_git_dir_paths`` and this no-follow open are
                # deliberately separate: replacing the path between them must
                # fail open, never redirect the recursive scan.
                _log.warning(
                    "agent.worktree_activity.subtree_unreadable",
                    worktree_path=str(self._worktree_path),
                    path=str(root.path),
                    error=str(exc),
                )
                for _path, pending_descriptor in stack:
                    os.close(pending_descriptor)
                return None
            stack.append((str(root.path), root_descriptor))
        budget = self._max_entries
        try:
            while stack:
                current, descriptor = stack.pop()
                try:
                    with os.scandir(descriptor) as entries:
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
                            entry_path = str(Path(current) / entry.name)
                            newest, fingerprint = _absorb(
                                newest,
                                fingerprint,
                                entry_path,
                                stat_result,
                            )
                            if stat_module.S_ISDIR(stat_result.st_mode):
                                child_descriptor = _open_observed_directory(
                                    entry.name,
                                    descriptor,
                                    stat_result,
                                )
                                stack.append((entry_path, child_descriptor))
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
                finally:
                    os.close(descriptor)
        finally:
            for _path, pending_descriptor in stack:
                os.close(pending_descriptor)
        return _Scan(newest_mtime=newest, fingerprint=fingerprint)

    def _git_dir_paths(self) -> _GitPaths | None:
        if not self._git_layout_pinned and not self._pin_current_git_layout():
            # Priming could not establish an external layout before execution.
            # A fixed lstat of the worktree's own `.git` is still safe: when it
            # is absent or a real directory, every Git path is already covered
            # by the worktree walk. A pointer or symlink remains unknown rather
            # than being followed after the agent could have replaced it.
            try:
                git_path_mode = (self._worktree_path / ".git").lstat().st_mode
            except FileNotFoundError:
                return _GitPaths((), ())
            if stat_module.S_ISDIR(git_path_mode):
                return _GitPaths((), ())
            return None
        layout = self._git_layout
        if layout is None:
            return _GitPaths((), ())
        git_root = layout.git_dir
        git_dir = git_root.path
        common_root = layout.common_dir
        common_dir = common_root.path
        if self._trusted_git_roots is not None:
            # Git consults these agent-writable files for every ref operation.
            # A rewrite after priming must make the scan indeterminate instead
            # of leaving the watches pinned to roots Git no longer uses.
            _require_trusted_git_marker(self._worktree_path, git_dir)
            _require_trusted_git_common_dir(git_root, common_dir)
        git_dir_watch = _WatchedPath(git_root, Path())
        git_dir_stat = _metadata_stat_at(git_dir_watch)
        if git_dir_stat is None:
            # An absent root is a complete observation and is not walked. If it
            # reappears, it cannot be trusted because no pre-agent identity was
            # captured for that new directory object.
            return _GitPaths((git_dir_watch,), ())
        if not _matches_directory_identity(git_dir_stat, git_root.identity):
            _log.warning(
                "agent.worktree_activity.git_root_replaced",
                worktree_path=str(self._worktree_path),
                path=str(git_dir),
            )
            return None
        # The git dir itself, so that per-worktree metadata with no watch of its
        # own — ``ORIG_HEAD``, ``MERGE_HEAD``, ``COMMIT_EDITMSG``, a
        # ``rebase-merge`` directory — registers when it appears or is replaced:
        # every one of those is created, renamed or removed inside this
        # directory, which moves its mtime.
        watched = [git_dir_watch]
        watched.extend(_WatchedPath(git_root, name) for name in _GIT_DIR_ACTIVITY_FILES)
        watched.append(_WatchedPath(git_root, _REFTABLE_STACK_FILE))
        # The git dir resolved above is this worktree's alone — a linked
        # worktree's private directory, or the one a ``.git`` symlink / pointer
        # file names for a checkout of its own — so it is walked whole, not just
        # stat-ed. Its own mtime only moves when a direct child appears or is
        # replaced, which leaves every write *inside* it (a rebase advancing
        # through ``rebase-merge/done``, a ``sequencer`` todo being rewritten, a
        # ``git tag`` or second branch landing under ``refs/``) and every
        # in-place rewrite of a file not named above invisible: an interval
        # whose only work was Git's would read as idleness. A plain ``.git``
        # *directory* is already walked whole by the worktree walk itself, and
        # going through a symlink — which the walk never descends — must not
        # quietly narrow that to the handful of names watched here.
        walk_roots = (git_root,)
        # The *common* dir stays out of the walk, and nothing shared under it is
        # watched by name either: one bare mirror backs every worktree of a
        # repo, so its churn is other workspaces' agents and would report this
        # one as alive whatever it is doing — and it carries the object store,
        # whose size would burn the walk budget. The common
        # ``reftable/tables.list`` is shared exactly that way: under the
        # reftable backend every ref transaction in *any* worktree of the repo
        # rewrites it, so a neighbouring workspace committing, tagging or
        # fetching would keep extending this idle agent to the wall cap. Only
        # the one path under the common dir that belongs to this worktree — the
        # branch ref HEAD names — is watched, by name; the reftable state that
        # is this worktree's alone lives in its own git dir, watched above.
        branch_ref = _resolve_head_branch_ref(git_root, common_dir)
        if branch_ref is not None:
            watched.append(_WatchedPath(common_root, branch_ref.relative_to(common_dir)))
        return _GitPaths(tuple(watched), walk_roots)


async def make_worktree_activity_probe(
    worktree_path: Path | None,
    *,
    trusted_git_roots: tuple[Path, Path] | None = None,
) -> ActivityProbe | None:
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
    probe = WorktreeActivityProbe(
        worktree_path,
        trusted_git_roots=trusted_git_roots,
    )
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

    ``logs/HEAD`` exists only once a reflog does, and a packed branch ref has no
    loose file until the next update writes one, so "not there" has to stay a
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


def _metadata_stat_at(watched: _WatchedPath) -> os.stat_result | None:
    """Stat a watched path beneath its pinned root without following symlinks.

    ``lstat(root / relative)`` protects only the final component; every earlier
    component is still followed. Open each intermediate directory relative to
    its already-open parent with ``O_NOFOLLOW`` so an agent cannot replace
    ``logs``, ``reftable``, or ``refs/heads`` with an outside-pointing symlink.
    A missing root or component is the same complete absence observation as
    :func:`_metadata_stat`; every other access failure propagates so the scan
    fails open.
    """
    root = watched.root
    if root.identity is None:
        if root.anchor is not None and root.relative_to_anchor is not None:
            try:
                appeared_descriptor = _open_directory_beneath(
                    root.anchor,
                    root.relative_to_anchor,
                )
            except FileNotFoundError:
                return None
            else:
                os.close(appeared_descriptor)
                raise OSError(
                    errno.ESTALE,
                    "pinned directory appeared after priming",
                    root.path,
                )
        root_stat = _metadata_stat(root.path)
        if root_stat is None:
            return None
        raise OSError(errno.ESTALE, "pinned directory appeared after priming", root.path)
    descriptors: list[int] = []
    try:
        descriptor = _open_pinned_directory(root)
        descriptors.append(descriptor)
        parts = watched.relative.parts
        if not parts:
            return os.fstat(descriptor)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        for component in parts[:-1]:
            descriptor = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        return os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_git_file_at(git_root: _PinnedDirectory, name: str) -> str:
    """Read one file directly beneath a pinned Git root without following it."""
    descriptor = _open_pinned_directory(git_root)
    try:
        file_descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        try:
            return os.read(file_descriptor, 4096).decode("utf-8", errors="replace")
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _read_head_at(git_root: _PinnedDirectory) -> str | None:
    """Read HEAD beneath its pinned root, preserving absence as complete."""
    try:
        return _read_git_file_at(git_root, "HEAD")
    except FileNotFoundError:
        return None


def _matches_directory_identity(
    stat_result: os.stat_result,
    identity: _DirectoryIdentity | None,
) -> bool:
    """Return whether ``stat_result`` is the pre-agent directory object."""
    return (
        identity is not None
        and stat_module.S_ISDIR(stat_result.st_mode)
        and stat_result.st_dev == identity.device
        and stat_result.st_ino == identity.inode
    )


def _pin_directory(path: Path) -> _PinnedDirectory:
    """Capture a directory identity, preserving a positively absent root."""
    stat_result = _metadata_stat(path)
    if stat_result is None:
        return _PinnedDirectory(path, None)
    if not stat_module.S_ISDIR(stat_result.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "Git admin root is not a directory", path)
    return _PinnedDirectory(
        path,
        _DirectoryIdentity(stat_result.st_dev, stat_result.st_ino),
    )


def _pin_directory_beneath(
    anchor: _PinnedDirectory,
    relative: Path,
) -> _PinnedDirectory:
    """Pin a directory reached beneath ``anchor`` without following symlinks."""
    path = anchor.path / relative
    try:
        descriptor = _open_directory_beneath(anchor, relative)
    except FileNotFoundError:
        return _PinnedDirectory(path, None, anchor, relative)
    try:
        stat_result = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return _PinnedDirectory(
        path,
        _DirectoryIdentity(stat_result.st_dev, stat_result.st_ino),
        anchor,
        relative,
    )


def _open_checked_directory(
    path: str | Path,
    expected: _DirectoryIdentity,
    *,
    parent_descriptor: int | None = None,
) -> int:
    """Open one exact directory object without following its final component."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = (
        os.open(path, flags)
        if parent_descriptor is None
        else os.open(path, flags, dir_fd=parent_descriptor)
    )
    try:
        opened = os.fstat(descriptor)
        if not _matches_directory_identity(opened, expected):
            raise OSError(errno.ESTALE, "directory identity changed", os.fspath(path))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_pinned_directory(directory: _PinnedDirectory) -> int:
    """Open a pre-agent directory, rejecting absence or replacement."""
    if directory.identity is None:
        raise OSError(errno.ESTALE, "directory was absent when pinned", directory.path)
    if directory.anchor is not None and directory.relative_to_anchor is not None:
        return _open_directory_beneath(
            directory.anchor,
            directory.relative_to_anchor,
            expected=directory.identity,
        )
    return _open_checked_directory(directory.path, directory.identity)


def _open_directory_beneath(
    anchor: _PinnedDirectory,
    relative: Path,
    *,
    expected: _DirectoryIdentity | None = None,
) -> int:
    """Open a relative directory chain beneath a pinned root without symlinks."""
    parts = relative.parts
    if not parts or relative.is_absolute():
        raise OSError(errno.EINVAL, "invalid anchored directory path", relative)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors = [_open_pinned_directory(anchor)]
    try:
        for component in parts:
            descriptors.append(os.open(component, flags, dir_fd=descriptors[-1]))
        result = descriptors.pop()
        try:
            matches_expected = expected is None or _matches_directory_identity(
                os.fstat(result),
                expected,
            )
        except BaseException:
            os.close(result)
            raise
        if not matches_expected:
            os.close(result)
            raise OSError(
                errno.ESTALE,
                "directory identity changed",
                anchor.path / relative,
            )
        return result
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _open_observed_directory(
    name: str,
    parent_descriptor: int,
    observed: os.stat_result,
) -> int:
    """Open a walked child without accepting a stat/open substitution."""
    expected = _DirectoryIdentity(observed.st_dev, observed.st_ino)
    return _open_checked_directory(
        name,
        expected,
        parent_descriptor=parent_descriptor,
    )


def _resolve_linked_git_dir(worktree_path: Path) -> Path | None:
    """Resolve a ``gitdir:`` pointer file to the real git dir, if present.

    A plain ``.git`` *directory* needs no special handling — the walk already
    covers it — so only the linked-worktree pointer file resolves here.

    A ``.git`` *symlink* to a git directory is not that plain case, even though
    ``is_dir`` follows the link and says it is. The walk stats entries with
    ``follow_symlinks=False`` and never descends through one, and a symlink's
    own lstat does not move when its target is written, so the git dir behind it
    is exactly as external to the walk as a linked worktree's pointer target —
    and resolves here the same way, or Git-only writes would leave the
    fingerprint unchanged and authorise an idle kill of an agent mid-commit.

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
            return git_path.resolve() if git_path.is_symlink() else None
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


def _lexical_absolute(path: Path) -> Path:
    """Make ``path`` absolute without following agent-controlled symlinks."""
    return Path(os.path.abspath(path))  # noqa: PTH100 - Path.resolve() follows symlinks.


def _require_trusted_git_marker(worktree_path: Path, expected_git_dir: Path) -> None:
    """Require ``.git`` to name GitManager's admin dir without following it.

    A later agent invocation inherits an agent-writable checkout, so resolving
    the marker's target and trusting whatever it names would let the prior
    invocation redirect this process into an unrelated tree. Read the marker
    itself with ``O_NOFOLLOW`` and compare its normalized pathname to the
    control-plane-derived path; neither operation resolves target symlinks.
    """
    git_path = worktree_path / ".git"
    observed = git_path.lstat()
    if not stat_module.S_ISREG(observed.st_mode):
        raise OSError(errno.ESTALE, "managed worktree .git marker changed type", git_path)
    descriptor = os.open(
        git_path,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or opened.st_dev != observed.st_dev
            or opened.st_ino != observed.st_ino
        ):
            raise OSError(errno.ESTALE, "managed worktree .git marker replaced", git_path)
        payload = os.read(descriptor, _GITFILE_MAX_BYTES + 1)
        if len(payload) > _GITFILE_MAX_BYTES:
            raise OSError(
                errno.ESTALE,
                "managed worktree .git marker exceeds read limit",
                git_path,
            )
        content = payload.decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)

    target: Path | None = None
    stripped = content.strip()
    if stripped.startswith(_GITDIR_PREFIX):
        raw = stripped[len(_GITDIR_PREFIX) :].strip()
        if raw:
            candidate = Path(raw)
            target = candidate if candidate.is_absolute() else worktree_path / candidate
    normalized_target = _lexical_absolute(target) if target is not None else None
    normalized_expected = _lexical_absolute(expected_git_dir)
    if normalized_target != normalized_expected:
        raise OSError(
            errno.ESTALE,
            "managed worktree .git marker does not match GitManager metadata",
            git_path,
        )


def _require_trusted_git_common_dir(
    git_root: _PinnedDirectory,
    expected_common_dir: Path,
) -> None:
    """Require the pinned admin dir's ``commondir`` to select GitManager's root."""
    content = _read_git_file_at(git_root, _GIT_COMMON_DIR_FILE)
    candidate = Path(content.strip() or ".")
    normalized_expected = _lexical_absolute(expected_common_dir)
    matches_expected = (
        candidate == normalized_expected
        if candidate.is_absolute()
        else all(part == os.pardir for part in candidate.parts)
        and _lexical_absolute(git_root.path / candidate) == normalized_expected
    )
    if not matches_expected:
        raise OSError(
            errno.ESTALE,
            "managed worktree commondir does not match GitManager metadata",
            git_root.path / _GIT_COMMON_DIR_FILE,
        )


def _resolve_git_layout(
    worktree_path: Path,
    *,
    trusted_git_roots: tuple[Path, Path] | None = None,
) -> _GitLayout | None:
    """Resolve and canonicalize the external Git roots captured before execution."""
    if trusted_git_roots is not None:
        expected_git_dir, expected_common_dir = trusted_git_roots
        trusted_git_dir = _lexical_absolute(expected_git_dir)
        trusted_common_dir = _lexical_absolute(expected_common_dir)
        try:
            git_dir_relative = trusted_git_dir.relative_to(trusted_common_dir)
        except ValueError as exc:
            raise OSError(
                errno.ESTALE,
                "GitManager activity admin dir escapes its common dir",
                trusted_git_dir,
            ) from exc
        if len(git_dir_relative.parts) != 2 or git_dir_relative.parts[0] != "worktrees":
            raise OSError(
                errno.ESTALE,
                "GitManager activity admin dir has an invalid managed layout",
                trusted_git_dir,
            )
        _require_trusted_git_marker(worktree_path, trusted_git_dir)
        common_root = _pin_directory(trusted_common_dir)
        git_root = _pin_directory_beneath(common_root, git_dir_relative)
        _require_trusted_git_common_dir(git_root, trusted_common_dir)
        return _GitLayout(
            git_dir=git_root,
            common_dir=common_root,
        )
    git_dir = _resolve_linked_git_dir(worktree_path)
    if git_dir is None:
        return None
    canonical_git_dir = git_dir.resolve()
    canonical_common_dir = _git_common_dir(canonical_git_dir).resolve()
    return _GitLayout(
        git_dir=_pin_directory(canonical_git_dir),
        common_dir=_pin_directory(canonical_common_dir),
    )


def _resolve_head_branch_ref(
    git_root: _PinnedDirectory,
    common_dir: Path,
) -> Path | None:
    """Resolve HEAD's symbolic target to the ref file a commit lands in.

    That file lives under the *common* git dir (``common_dir``), not the linked
    worktree's own, and it is the one thing a files-backend commit is guaranteed
    to move: the worktree's ``HEAD`` keeps naming the same branch,
    ``core.logAllRefUpdates=false`` writes no ``logs/HEAD``, and an index already
    matching the previous probe — staged during the preceding idle window, say —
    is not rewritten either. Left out, such a commit reads as idleness and the
    watchdog kills the agent moments after it committed.

    ``None`` is a *complete* observation: a detached HEAD names no branch, and
    the commit moves the watched ``HEAD`` file itself; an absent or unparsable
    HEAD names none either. A HEAD that exists but cannot be read is not — the
    branch it names may be moving without this process being able to see it — so
    the ``OSError`` propagates and the caller marks the scan incomplete.

    The ref file's own *absence* stays complete, handled like ``logs/HEAD``:
    a packed ref has no loose file until the next update writes one. Under the
    ``reftable`` backend it never gets one at all — HEAD is a stub naming
    ``refs/heads/.invalid`` — which is why the caller watches this worktree's
    own ``reftable/tables.list`` alongside whatever this resolves to.
    """
    head = _read_head_at(git_root)
    if head is None:
        return None
    target = head.partition("\n")[0].strip()
    if not target.startswith(_HEAD_REF_PREFIX):
        return None
    ref = target[len(_HEAD_REF_PREFIX) :].strip()
    if not ref:
        return None
    ref_path = Path(ref)
    if ref_path.is_absolute():
        return None
    branch_ref = Path(os.path.normpath(common_dir / ref_path))
    if not branch_ref.is_relative_to(common_dir):
        return None
    return branch_ref


def _git_common_dir(git_dir: Path) -> Path:
    """Resolve ``commondir``, where a linked worktree's refs actually live.

    Absent — a plain git dir, including the one behind a ``.git`` symlink —
    means the git dir already is the common one. Unreadable propagates, like
    every other observation this scan cannot complete.
    """
    try:
        content = (git_dir / _GIT_COMMON_DIR_FILE).read_text(
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return git_dir
    raw = content.partition("\n")[0].strip()
    if not raw:
        return git_dir
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else git_dir / candidate
