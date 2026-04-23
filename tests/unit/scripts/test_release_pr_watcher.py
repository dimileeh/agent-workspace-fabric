"""Tests for ``scripts.release_pr_watcher`` — the polling daemon that
opens dev→main release PRs and (optionally) attaches monitors.

We don't start the real watcher loop; we exercise each unit
(``_tick_one``, ``_monitor_already_running``, ``_run``) with
monkey-patched collaborators so the tests don't need network or
subprocess I/O."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import release_pr_watcher

# ── _monitor_already_running ───────────────────────────────────────────────


class TestMonitorAlreadyRunning:
    @pytest.mark.unit
    def test_true_when_matching_process_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A running run_awf.py with the exact spec filename in its
        argv counts as "monitor already running" — don't launch a
        second one."""
        spec_fragment = "dimileeh__aira-web-pr278.json"

        def _fake_check_output(*args: Any, **kwargs: Any) -> str:
            return (
                f"12345 python scripts/run_awf.py --config "
                f"/tmp/specs/{spec_fragment} --work-dir /tmp/awf\n"
                "67890 python something_else.py\n"
            )

        monkeypatch.setattr(subprocess, "check_output", _fake_check_output)
        assert release_pr_watcher._monitor_already_running(
            work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=278
        )

    @pytest.mark.unit
    def test_false_when_no_matching_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda *_a, **_k: "12345 python unrelated.py\n",
        )
        assert not release_pr_watcher._monitor_already_running(
            work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=999
        )

    @pytest.mark.unit
    def test_false_on_pgrep_missing_or_no_matches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """pgrep exits 1 when there are no matches. That's not an
        error — it's an empty result."""

        def _raise(*a: Any, **k: Any) -> str:
            raise subprocess.CalledProcessError(1, "pgrep")

        monkeypatch.setattr(subprocess, "check_output", _raise)
        assert not release_pr_watcher._monitor_already_running(
            work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=1
        )

    @pytest.mark.unit
    def test_false_when_pgrep_not_installed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Some hosts don't ship pgrep. Must not crash the watcher."""

        def _raise(*a: Any, **k: Any) -> str:
            raise FileNotFoundError("pgrep")

        monkeypatch.setattr(subprocess, "check_output", _raise)
        assert not release_pr_watcher._monitor_already_running(
            work_dir=tmp_path, repo_slug="dimileeh/aira-web", pr_number=1
        )


# ── _tick_one ──────────────────────────────────────────────────────────────


class _FakeEnsureResult:
    def __init__(
        self,
        *,
        ahead_by: int = 3,
        pr_number: int | None = 42,
        created: bool = False,
        reason: str = "already_open",
    ) -> None:
        self.ahead_by = ahead_by
        self.pr_number = pr_number
        self.created = created
        self.reason = reason


@pytest.fixture
def stub_ensure(monkeypatch: pytest.MonkeyPatch):
    """Replace ``ensure_release_pr_open`` with a recorder that returns
    a canned result. Tests set ``stub.result`` before calling
    ``_tick_one``."""

    class _Stub:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.result: Any = _FakeEnsureResult()
            self.raise_error: Exception | None = None

        async def __call__(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            if self.raise_error is not None:
                raise self.raise_error
            return self.result

    stub = _Stub()
    monkeypatch.setattr(release_pr_watcher, "ensure_release_pr_open", stub)
    return stub


@pytest.fixture
def stub_subprocess_exec(monkeypatch: pytest.MonkeyPatch):
    """Replace ``asyncio.create_subprocess_exec`` with a recorder that
    returns a canned-outcome coroutine. Tests can set
    ``stub.returncode`` / ``stub.stderr`` before invoking."""

    class _FakeProc:
        def __init__(self, *, returncode: int, stdout: bytes, stderr: bytes) -> None:
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self) -> tuple[bytes, bytes]:
            return self._stdout, self._stderr

    class _Stub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []
            self.returncode: int = 0
            self.stdout: bytes = b"scheduled"
            self.stderr: bytes = b""

        async def __call__(self, *args: str, **kwargs: Any) -> _FakeProc:
            self.calls.append(args)
            return _FakeProc(
                returncode=self.returncode,
                stdout=self.stdout,
                stderr=self.stderr,
            )

    stub = _Stub()
    monkeypatch.setattr(release_pr_watcher.asyncio, "create_subprocess_exec", stub)
    return stub


class _NoopRunner:
    pass


class TestTickOne:
    @pytest.mark.unit
    async def test_sync_error_logged_and_returns_without_spawning(
        self,
        stub_ensure: Any,
        stub_subprocess_exec: Any,
        tmp_path: Path,
    ) -> None:
        """A ReleasePrSyncError on a single repo is swallowed so the
        next tick can retry. No scheduler process is spawned."""
        stub_ensure.raise_error = release_pr_watcher.ReleasePrSyncError(
            operation="gh pr list",
            stderr="auth token expired",
        )
        await release_pr_watcher._tick_one(
            runner=_NoopRunner(),  # type: ignore[arg-type]
            repo_url="git@github.com:x/y.git",
            source="development",
            target="main",
            work_dir=tmp_path,
            attach_monitor=True,
            agent="codex",
            companions_path=None,
        )
        assert stub_subprocess_exec.calls == []

    @pytest.mark.unit
    async def test_attach_monitor_disabled_skips_scheduler(
        self,
        stub_ensure: Any,
        stub_subprocess_exec: Any,
        tmp_path: Path,
    ) -> None:
        stub_ensure.raise_error = None
        stub_ensure.result = _FakeEnsureResult(pr_number=100)
        await release_pr_watcher._tick_one(
            runner=_NoopRunner(),  # type: ignore[arg-type]
            repo_url="git@github.com:x/y.git",
            source="development",
            target="main",
            work_dir=tmp_path,
            attach_monitor=False,
            agent="codex",
            companions_path=None,
        )
        assert stub_subprocess_exec.calls == []

    @pytest.mark.unit
    async def test_no_pr_skips_scheduler(
        self,
        stub_ensure: Any,
        stub_subprocess_exec: Any,
        tmp_path: Path,
    ) -> None:
        """``ensure_release_pr_open`` can return ``pr_number=None`` when
        no PR is needed (e.g. source not ahead of target)."""
        stub_ensure.raise_error = None
        stub_ensure.result = _FakeEnsureResult(ahead_by=0, pr_number=None, reason="no_diff")
        await release_pr_watcher._tick_one(
            runner=_NoopRunner(),  # type: ignore[arg-type]
            repo_url="git@github.com:x/y.git",
            source="development",
            target="main",
            work_dir=tmp_path,
            attach_monitor=True,
            agent="codex",
            companions_path=None,
        )
        assert stub_subprocess_exec.calls == []

    @pytest.mark.unit
    async def test_monitor_already_running_skips_spawn(
        self,
        stub_ensure: Any,
        stub_subprocess_exec: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        stub_ensure.raise_error = None
        stub_ensure.result = _FakeEnsureResult(pr_number=500)
        monkeypatch.setattr(
            release_pr_watcher,
            "_monitor_already_running",
            lambda **_k: True,
        )
        await release_pr_watcher._tick_one(
            runner=_NoopRunner(),  # type: ignore[arg-type]
            repo_url="git@github.com:x/y.git",
            source="development",
            target="main",
            work_dir=tmp_path,
            attach_monitor=True,
            agent="codex",
            companions_path=None,
        )
        assert stub_subprocess_exec.calls == []

    @pytest.mark.unit
    async def test_spawns_scheduler_when_needed(
        self,
        stub_ensure: Any,
        stub_subprocess_exec: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        stub_ensure.raise_error = None
        stub_ensure.result = _FakeEnsureResult(pr_number=789, created=True)
        monkeypatch.setattr(
            release_pr_watcher,
            "_monitor_already_running",
            lambda **_k: False,
        )
        await release_pr_watcher._tick_one(
            runner=_NoopRunner(),  # type: ignore[arg-type]
            repo_url="git@github.com:x/y.git",
            source="development",
            target="main",
            work_dir=tmp_path,
            attach_monitor=True,
            agent="codex",
            companions_path=None,
        )
        assert len(stub_subprocess_exec.calls) == 1
        args = stub_subprocess_exec.calls[0]
        assert "--attach-monitor" in args
        assert "--repo" in args
        assert "git@github.com:x/y.git" in args
        assert "--source" in args and "development" in args
        assert "--target" in args and "main" in args
        assert "--agent" in args and "codex" in args
        # --companions only when path is provided.
        assert "--companions" not in args

    @pytest.mark.unit
    async def test_companions_path_flag_added_when_set(
        self,
        stub_ensure: Any,
        stub_subprocess_exec: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        stub_ensure.raise_error = None
        stub_ensure.result = _FakeEnsureResult(pr_number=42, created=True)
        monkeypatch.setattr(
            release_pr_watcher,
            "_monitor_already_running",
            lambda **_k: False,
        )
        companions = tmp_path / "companions.json"
        companions.write_text("[]")
        await release_pr_watcher._tick_one(
            runner=_NoopRunner(),  # type: ignore[arg-type]
            repo_url="git@github.com:x/y.git",
            source="development",
            target="main",
            work_dir=tmp_path,
            attach_monitor=True,
            agent="codex",
            companions_path=companions,
        )
        args = stub_subprocess_exec.calls[0]
        assert "--companions" in args
        assert str(companions) in args

    @pytest.mark.unit
    async def test_scheduler_nonzero_exit_logged_not_raised(
        self,
        stub_ensure: Any,
        stub_subprocess_exec: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """If the scheduler exits non-zero, the watcher must not crash.
        Log and move on — next tick retries."""
        stub_ensure.raise_error = None
        stub_ensure.result = _FakeEnsureResult(pr_number=42, created=True)
        monkeypatch.setattr(
            release_pr_watcher,
            "_monitor_already_running",
            lambda **_k: False,
        )
        stub_subprocess_exec.returncode = 2
        stub_subprocess_exec.stderr = b"spec file not writable"
        await release_pr_watcher._tick_one(
            runner=_NoopRunner(),  # type: ignore[arg-type]
            repo_url="git@github.com:x/y.git",
            source="development",
            target="main",
            work_dir=tmp_path,
            attach_monitor=True,
            agent="codex",
            companions_path=None,
        )
        # The scheduler DID run (one spawn), and the watcher returned
        # without raising despite the non-zero exit.
        assert len(stub_subprocess_exec.calls) == 1


# ── _run loop ──────────────────────────────────────────────────────────────


class TestRunLoop:
    @pytest.mark.unit
    async def test_loop_exits_when_stop_signalled_after_one_tick(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The main loop must be interruptible: each iteration ticks
        all repos, then ``asyncio.wait_for(stop.wait(), interval)``
        gets us out of sleep as soon as the stop event fires.

        We drive this by replacing ``_tick_one`` with a function that
        signals stop on its first call, so the loop exits after one
        iteration."""
        stop_from_outside: asyncio.Event | None = None

        # Capture the stop event created inside _run by overriding
        # asyncio.Event. There's no cleaner seam short of refactoring
        # _run to accept an injected event — fine here.
        orig_event = asyncio.Event

        def _capture_event() -> asyncio.Event:
            nonlocal stop_from_outside
            e = orig_event()
            stop_from_outside = e
            return e

        tick_calls: list[str] = []

        async def _fake_tick(*, repo_url: str, **kwargs: Any) -> None:
            tick_calls.append(repo_url)
            # Signal stop from inside the tick so the loop exits after
            # this iteration completes.
            assert stop_from_outside is not None
            stop_from_outside.set()

        monkeypatch.setattr(release_pr_watcher.asyncio, "Event", _capture_event)
        monkeypatch.setattr(release_pr_watcher, "_tick_one", _fake_tick)

        rc = await release_pr_watcher._run(
            repos=["git@github.com:x/a.git", "git@github.com:x/b.git"],
            source="development",
            target="main",
            interval=0.01,
            work_dir=tmp_path,
            attach_monitor=False,
            agent="codex",
            companions_path=None,
        )
        assert rc == 0
        assert tick_calls == ["git@github.com:x/a.git", "git@github.com:x/b.git"]

    @pytest.mark.unit
    async def test_interval_timeout_path_runs_another_tick(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Covers the ``except TimeoutError`` branch: when the sleep
        window elapses without stop being set, the loop re-ticks. We
        stop on the second tick so the test terminates deterministically."""
        stop_from_outside: asyncio.Event | None = None
        orig_event = asyncio.Event

        def _capture_event() -> asyncio.Event:
            nonlocal stop_from_outside
            e = orig_event()
            stop_from_outside = e
            return e

        tick_count = [0]

        async def _fake_tick(**kwargs: Any) -> None:
            tick_count[0] += 1
            if tick_count[0] >= 2:
                assert stop_from_outside is not None
                stop_from_outside.set()

        monkeypatch.setattr(release_pr_watcher.asyncio, "Event", _capture_event)
        monkeypatch.setattr(release_pr_watcher, "_tick_one", _fake_tick)

        rc = await release_pr_watcher._run(
            repos=["git@github.com:x/a.git"],
            source="development",
            target="main",
            interval=0.01,  # short so the timeout branch fires fast
            work_dir=tmp_path,
            attach_monitor=False,
            agent="codex",
            companions_path=None,
        )
        assert rc == 0
        assert tick_count[0] == 2

    @pytest.mark.unit
    async def test_signal_handler_sets_stop_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Covers the signal-handler body (lines 185-186). We capture
        the handler that ``_run`` registers via ``signal.signal``, then
        invoke it with a fake signum and assert it sets the stop event.
        """
        import signal

        captured: dict[str, Any] = {}
        real_signal = signal.signal

        def _capture(signum: int, handler: Any) -> Any:
            if signum in (signal.SIGTERM, signal.SIGINT):
                captured[signum] = handler
            return real_signal(signum, handler)

        monkeypatch.setattr(signal, "signal", _capture)

        stop_from_outside: asyncio.Event | None = None
        orig_event = asyncio.Event

        def _capture_event() -> asyncio.Event:
            nonlocal stop_from_outside
            e = orig_event()
            stop_from_outside = e
            return e

        async def _tick_once_then_verify_and_stop(**kwargs: Any) -> None:
            # By now the handler has been registered. Call it with a
            # fake signum — the handler should set stop.
            assert stop_from_outside is not None
            assert not stop_from_outside.is_set()
            handler = captured[signal.SIGTERM]
            handler(signal.SIGTERM, None)
            assert stop_from_outside.is_set()

        monkeypatch.setattr(release_pr_watcher.asyncio, "Event", _capture_event)
        monkeypatch.setattr(release_pr_watcher, "_tick_one", _tick_once_then_verify_and_stop)

        rc = await release_pr_watcher._run(
            repos=["git@github.com:x/a.git"],
            source="development",
            target="main",
            interval=10.0,  # big; we rely on stop, not timeout
            work_dir=tmp_path,
            attach_monitor=False,
            agent="codex",
            companions_path=None,
        )
        assert rc == 0
