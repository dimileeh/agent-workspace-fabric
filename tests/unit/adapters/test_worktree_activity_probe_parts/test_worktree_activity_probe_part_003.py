"""Worktree activity probe: scan threads and worker-wide ceiling (continued from part 002).

Scans run on dedicated threads that never occupy the shared default executor and
never block interpreter shutdown; an abandoned scan keeps its slot until its own
thread settles, and the worker-wide ceiling caps how many run at once.

Split out of ``tests/unit/adapters/test_worktree_activity_probe.py`` to keep
each test module under the first-party 1500-line maintainability guardrail.
"""

from __future__ import annotations

import asyncio
import concurrent.futures.thread
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import structlog

from awf.adapters import worktree_activity
from awf.adapters.worktree_activity import (
    WorktreeActivityProbe,
    make_worktree_activity_probe,
)
from tests.unit.adapters.test_worktree_activity_probe_parts.helpers import (
    SRC_ROOT,
    _await_scan_gate,
)


@pytest.mark.unit
@pytest.mark.parametrize("cancelled", [False, True])
async def test_scan_returns_slot_to_its_original_counter(
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    owner = worktree_activity._LiveScanThreads(1)
    replacement = worktree_activity._LiveScanThreads(1)
    pending: list[Callable[[], None]] = []
    monkeypatch.setattr(worktree_activity, "_live_scan_threads", owner)
    monkeypatch.setattr(worktree_activity, "_start_scan_thread", pending.append)
    scan = asyncio.create_task(
        worktree_activity._run_scan(
            lambda: "scanned", worktree_path="/ws/counter-owner", gate=worktree_activity._ScanGate()
        )
    )
    await asyncio.sleep(0)
    assert owner._live == 1
    if cancelled:
        scan.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scan
        await asyncio.sleep(0)
    monkeypatch.setattr(worktree_activity, "_live_scan_threads", replacement)
    assert len(pending) == 1
    pending[0]()
    if not cancelled:
        assert await scan == "scanned"
    assert owner._live == 0
    assert replacement._live == 0


@pytest.mark.unit
async def test_scan_start_failure_returns_slot_to_its_original_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = worktree_activity._LiveScanThreads(1)
    replacement = worktree_activity._LiveScanThreads(1)
    monkeypatch.setattr(worktree_activity, "_live_scan_threads", owner)

    def fail_start(_deliver: Callable[[], None]) -> None:
        assert owner._live == 1
        monkeypatch.setattr(worktree_activity, "_live_scan_threads", replacement)
        raise RuntimeError("cannot start scan thread")

    monkeypatch.setattr(worktree_activity, "_start_scan_thread", fail_start)
    with pytest.raises(worktree_activity._ScanCapacityError, match="cannot start scan thread"):
        await worktree_activity._run_scan(
            lambda: "scanned", worktree_path="/ws/start-failure", gate=worktree_activity._ScanGate()
        )
    assert owner._live == 0
    assert replacement._live == 0


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
        fresh = [
            thread
            for thread in threading.enumerate()
            if thread not in before and thread.name.startswith("awf-worktree-scan")
        ]
        assert len(fresh) == 1
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
async def test_finished_scans_return_their_worker_wide_slot(
    worktree: Path, settle_scan_threads: Callable[[], None]
) -> None:
    """The ceiling counts *live* threads, so ordinary probing cannot drain it.

    A slot leaked per completed scan would wedge every worktree on the worker
    after a few hundred quiet idle windows — the watchdog reading "could not
    tell" as activity for the rest of every run.
    """
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None

    assert await probe() is False
    assert await probe() is False

    settle_scan_threads()
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
    settle_scan_threads: Callable[[], None],
) -> None:
    """``Thread.start`` can still fail below the ceiling; that must not latch.

    Nothing runs the deliver callback in that case, so the reserved slot and the
    worktree's own gate have to be released here — otherwise one transient
    thread exhaustion would blind this probe, and leak a slot from the worker's
    budget, for the rest of the process.
    """
    probe = await make_worktree_activity_probe(worktree)
    assert probe is not None
    settle_scan_threads()

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
