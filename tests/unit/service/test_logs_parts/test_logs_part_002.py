"""Local service log helper tests."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import awf.service.logs as logs_mod
from awf.service.config import COMPOSE_ENV_FILE_OMITTED, LOCAL_SERVICE_COMPOSE_ENV_FILE
from awf.service.logs import (
    DEFAULT_LOG_TAIL,
    LOCAL_SERVICE_COMPOSE_FILE,
    ServiceLogName,
    ServiceLogsError,
    ServiceLogsResult,
    _resolve_local_service_compose_file,
    _run_subprocess,
    _service_log_secret_values,
    run_service_logs,
)


@pytest.fixture
def _default_local_service_compose_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}")
    monkeypatch.chdir(tmp_path)


def _write_compose_file(tmp_path: Path, contents: str) -> Path:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(contents, encoding="utf-8")
    return compose_file


@pytest.mark.unit
def test_service_logs_reloads_compose_interpolation_keys_when_file_stat_metadata_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first_contents = """
services:
  api:
    environment:
      FIRST: "${AWF_FIRST_TOKEN:?set AWF_FIRST_TOKEN}"
"""
    second_contents = """
services:
  api:
    environment:
      THIRD: "${AWF_THIRD_TOKEN:?set AWF_THIRD_TOKEN}"
"""
    assert len(first_contents.encode()) == len(second_contents.encode())
    compose_file = _write_compose_file(tmp_path, first_contents)
    fixed_mtime_ns = 1_700_000_000_000_000_000
    os.utime(compose_file, ns=(fixed_mtime_ns, fixed_mtime_ns))
    subprocess_calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.delenv("AWF_FIRST_TOKEN", raising=False)
    monkeypatch.delenv("AWF_THIRD_TOKEN", raising=False)
    service_environ = {
        "AWF_FIRST_TOKEN": "first-token",
        "AWF_THIRD_TOKEN": "third-token",
    }

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )
    compose_file.write_text(second_contents, encoding="utf-8")
    os.utime(compose_file, ns=(fixed_mtime_ns, fixed_mtime_ns))
    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )

    first_env = subprocess_calls[0]["env"]
    second_env = subprocess_calls[1]["env"]
    assert isinstance(first_env, dict)
    assert isinstance(second_env, dict)
    assert first_env["AWF_FIRST_TOKEN"] == "first-token"
    assert "AWF_THIRD_TOKEN" not in first_env
    assert second_env["AWF_THIRD_TOKEN"] == "third-token"
    assert "AWF_FIRST_TOKEN" not in second_env


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_failure_mentions_terminal_output() -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs == {"check": False, "capture_output": False, "text": True, "env": None}
        return subprocess.CompletedProcess(args, returncode=17, stdout=None, stderr=None)

    with pytest.raises(ServiceLogsError) as exc_info:
        run_service_logs(
            services=[ServiceLogName.api],
            follow=True,
            run_subprocess=_run,
        )

    assert exc_info.value.returncode == 17
    assert exc_info.value.detail == (
        "docker compose logs --follow exited with a non-zero status; "
        "docker output was already streamed to the terminal"
    )


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
@pytest.mark.parametrize("returncode", [128 + signal.SIGINT, -signal.SIGINT])
def test_service_logs_follow_interrupt_return_codes_are_success(returncode: int) -> None:
    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=returncode, stdout=None, stderr=None)

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
        run_subprocess=_run,
    )

    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_keyboard_interrupt_returns_empty_result() -> None:
    def _run(_args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise KeyboardInterrupt

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
        run_subprocess=_run,
    )

    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
@pytest.mark.parametrize(
    ("timeout_on_terminate", "expected_wait_timeouts", "expected_killed"),
    [
        (False, [None, 5.0], False),
        (True, [None, 5.0, None], True),
    ],
)
def test_service_logs_follow_keyboard_interrupt_reaps_default_process(
    monkeypatch: pytest.MonkeyPatch,
    timeout_on_terminate: bool,
    expected_wait_timeouts: list[float | None],
    expected_killed: bool,
) -> None:
    """Verify followed log processes are reaped when waiting is interrupted."""

    class _InterruptingFollowProcess:
        """Fake follow process that interrupts the first wait call."""

        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def __init__(self) -> None:
            """Initialize cleanup state captured by the assertions."""
            self.terminated = False
            self.killed = False
            self.wait_timeouts: list[float | None] = []

        def wait(self, timeout: float | None = None) -> int:
            """Record waits and simulate interrupt or terminate timeout paths."""
            self.wait_timeouts.append(timeout)
            if len(self.wait_timeouts) == 1:
                raise KeyboardInterrupt
            if timeout_on_terminate and len(self.wait_timeouts) == 2:
                raise subprocess.TimeoutExpired(cmd=["docker"], timeout=timeout)
            return -signal.SIGKILL if self.killed else -signal.SIGTERM

        def terminate(self) -> None:
            """Record that graceful termination was requested."""
            self.terminated = True

        def kill(self) -> None:
            """Record that forceful process cleanup was requested."""
            self.killed = True

    processes: list[_InterruptingFollowProcess] = []

    def _popen(_args: list[str], **kwargs: object) -> _InterruptingFollowProcess:
        """Return the fake process while preserving Popen pipe assertions."""
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        process = _InterruptingFollowProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
    )

    assert result.stdout == ""
    assert result.stderr == ""
    assert len(processes) == 1
    assert processes[0].terminated is True
    assert processes[0].killed is expected_killed
    assert processes[0].wait_timeouts == expected_wait_timeouts


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_broken_stdout_pipe_terminates_default_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed downstream stdout pipe must not leave the followed process running."""

    class _BrokenFlushSink:
        """Sink that accepts writes but fails when flushed."""

        def write(self, text: str) -> int:
            """Accept streamed text before simulating a closed pipe on flush."""
            return len(text)

        def flush(self) -> None:
            """Raise the downstream pipe closure seen by a streaming writer."""
            raise BrokenPipeError

    class _FollowProcess:
        """Follow process double that waits for explicit termination."""

        stdout = io.StringIO("line before downstream closes\n")
        stderr = io.StringIO("")

        def __init__(self) -> None:
            """Track termination and kill calls made by the streaming runner."""
            self.terminated = threading.Event()
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            """Return only after the runner terminates the followed process."""
            if not self.terminated.wait(0.25):
                raise AssertionError(
                    "follow process was not terminated after downstream stdout closed"
                )
            return -signal.SIGTERM

        def terminate(self) -> None:
            """Record graceful termination from the streaming runner."""
            self.terminated.set()

        def kill(self) -> None:
            """Record forced termination from the streaming runner."""
            self.killed = True
            self.terminated.set()

    processes: list[_FollowProcess] = []

    def _popen(_args: list[str], **kwargs: object) -> _FollowProcess:
        """Create a follow-process double with piped stdout and stderr."""
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        process = _FollowProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(sys, "stdout", _BrokenFlushSink())

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
    )

    assert result == ServiceLogsResult(stdout="", stderr="")
    assert len(processes) == 1
    assert processes[0].terminated.is_set()
    assert processes[0].killed is False


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
@pytest.mark.parametrize("failure_type", [OSError, ValueError])
def test_service_logs_follow_downstream_stdout_error_terminates_default_process(
    failure_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downstream stdout write failure must not leave the followed process running."""

    class _FailingFlushSink:
        """Sink that accepts writes but fails with a downstream error on flush."""

        def write(self, text: str) -> int:
            """Accept streamed text before simulating a downstream I/O failure."""
            return len(text)

        def flush(self) -> None:
            """Raise a non-BrokenPipe write failure variant."""
            raise failure_type("downstream stdout unavailable")

    class _FollowProcess:
        """Follow process double that waits for explicit termination."""

        stdout = io.StringIO("line before downstream errors\n")
        stderr = io.StringIO("")

        def __init__(self) -> None:
            """Track termination and kill calls made by the streaming runner."""
            self.terminated = threading.Event()
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            """Return only after the runner terminates the followed process."""
            if not self.terminated.wait(0.25):
                raise AssertionError(
                    "follow process was not terminated after downstream stdout error"
                )
            return -signal.SIGTERM

        def terminate(self) -> None:
            """Record graceful termination from the streaming runner."""
            self.terminated.set()

        def kill(self) -> None:
            """Record forced termination from the streaming runner."""
            self.killed = True
            self.terminated.set()

    processes: list[_FollowProcess] = []

    def _popen(_args: list[str], **kwargs: object) -> _FollowProcess:
        """Create a follow-process double with piped stdout and stderr."""
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        process = _FollowProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(sys, "stdout", _FailingFlushSink())

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
    )

    assert result == ServiceLogsResult(stdout="", stderr="")
    assert len(processes) == 1
    assert processes[0].terminated.is_set()
    assert processes[0].killed is False


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_blocked_downstream_write_terminates_default_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full downstream stdout pipe must not leave the followed process running."""

    class _BlockedWriteSink:
        """Sink that blocks a write while keeping the downstream pipe open."""

        def __init__(self) -> None:
            """Initialize synchronization points for the blocked write."""
            self.write_started = threading.Event()
            self.release_write = threading.Event()

        def write(self, text: str) -> int:
            """Block long enough for the streaming runner to detect the stall."""
            self.write_started.set()
            self.release_write.wait(timeout=0.5)
            return len(text)

        def flush(self) -> None:
            """Flush successfully when the blocked write is released."""

    class _FollowProcess:
        """Follow process double that must be terminated while stdout is blocked."""

        stdout = io.StringIO("line before downstream blocks\n")
        stderr = io.StringIO("")

        def __init__(self) -> None:
            """Track termination and kill calls made by the streaming runner."""
            self.terminated = threading.Event()
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            """Return only after the runner terminates the followed process."""
            wait_timeout = 0.25 if timeout is None else timeout
            if not self.terminated.wait(wait_timeout):
                raise AssertionError(
                    "follow process was not terminated after downstream stdout blocked"
                )
            return -signal.SIGTERM

        def terminate(self) -> None:
            """Record graceful termination from the streaming runner."""
            self.terminated.set()

        def kill(self) -> None:
            """Record forced termination from the streaming runner."""
            self.killed = True
            self.terminated.set()

    processes: list[_FollowProcess] = []

    def _popen(_args: list[str], **kwargs: object) -> _FollowProcess:
        """Create a follow-process double with piped stdout and stderr."""
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        process = _FollowProcess()
        processes.append(process)
        return process

    sink = _BlockedWriteSink()
    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(sys, "stdout", sink)
    monkeypatch.setattr(
        "awf.service.logs._STREAMING_DOWNSTREAM_WRITE_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(
        "awf.service.logs._STREAMING_DOWNSTREAM_WRITE_POLL_SECONDS",
        0.005,
        raising=False,
    )

    try:
        result = run_service_logs(
            services=[ServiceLogName.api],
            follow=True,
        )
    finally:
        sink.release_write.set()

    assert result == ServiceLogsResult(stdout="", stderr="")
    assert sink.write_started.is_set()
    assert len(processes) == 1
    assert processes[0].terminated.is_set()
    assert processes[0].killed is False


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_simultaneous_broken_pipes_terminate_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent downstream pipe closures should not race subprocess cleanup."""

    class _BrokenFlushSink:
        """Sink that waits for both stream threads before failing flush."""

        def __init__(self, barrier: threading.Barrier) -> None:
            """Initialize the shared synchronization point."""
            self._barrier = barrier

        def write(self, text: str) -> int:
            """Accept streamed text before simulating a closed pipe."""
            return len(text)

        def flush(self) -> None:
            """Raise after both stdout and stderr reach their downstream sink."""
            self._barrier.wait(timeout=1.0)
            raise BrokenPipeError

    class _FollowProcess:
        """Follow process double that records cleanup calls."""

        stdout = io.StringIO("stdout before downstream closes\n")
        stderr = io.StringIO("stderr before downstream closes\n")

        def __init__(self) -> None:
            """Track termination calls made by racing stream threads."""
            self.terminated = threading.Event()
            self.terminate_count = 0
            self.killed = False
            self._lock = threading.Lock()

        def wait(self, timeout: float | None = None) -> int:
            """Return only after the runner terminates the followed process."""
            wait_timeout = 0.25 if timeout is None else timeout
            if not self.terminated.wait(wait_timeout):
                raise AssertionError(
                    "follow process was not terminated after downstream pipes closed"
                )
            return -signal.SIGTERM

        def terminate(self) -> None:
            """Record graceful termination from the streaming runner."""
            with self._lock:
                self.terminate_count += 1
            self.terminated.set()

        def kill(self) -> None:
            """Record forced termination from the streaming runner."""
            self.killed = True
            self.terminated.set()

    processes: list[_FollowProcess] = []

    def _popen(_args: list[str], **kwargs: object) -> _FollowProcess:
        """Create a follow-process double with piped stdout and stderr."""
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        process = _FollowProcess()
        processes.append(process)
        return process

    barrier = threading.Barrier(2)
    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(sys, "stdout", _BrokenFlushSink(barrier))
    monkeypatch.setattr(sys, "stderr", _BrokenFlushSink(barrier))

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
    )

    assert result == ServiceLogsResult(stdout="", stderr="")
    assert len(processes) == 1
    assert processes[0].terminate_count == 1
    assert processes[0].killed is False


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_joins_peer_stream_after_watchdog_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give a peer stream one more bounded drain after a broken-pipe teardown."""

    class _BrokenFlushSink:
        """Sink that fails after the peer stream enters its downstream write."""

        def __init__(self, peer_write_started: threading.Event) -> None:
            """Initialize the peer-stream synchronization point."""
            self._peer_write_started = peer_write_started

        def write(self, text: str) -> int:
            """Accept streamed text before simulating a closed pipe."""
            return len(text)

        def flush(self) -> None:
            """Raise a downstream pipe closure while the peer stream is active."""
            if not self._peer_write_started.wait(timeout=1.0):
                raise AssertionError("peer stream did not enter downstream write")
            raise BrokenPipeError

    class _PeerSink:
        """Sink that stays in write until the post-watchdog join releases it."""

        def __init__(self) -> None:
            """Initialize peer stream write synchronization."""
            self.write_started = threading.Event()
            self.release_write = threading.Event()
            self.write_finished = threading.Event()

        def write(self, text: str) -> int:
            """Block in the peer write until teardown reaches the extra join."""
            self.write_started.set()
            self.release_write.wait(timeout=1.0)
            self.write_finished.set()
            return len(text)

        def flush(self) -> None:
            """Flush succeeds after the peer write drains."""

    class _FollowProcess:
        """Follow process double that records cleanup calls."""

        stdout = io.StringIO("stdout before downstream closes\n")
        stderr = io.StringIO("stderr already read before teardown\n")

        def __init__(self) -> None:
            """Track termination calls made by the streaming runner."""
            self.terminated = threading.Event()
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            """Return only after the runner terminates the followed process."""
            wait_timeout = 0.25 if timeout is None else timeout
            if not self.terminated.wait(wait_timeout):
                raise AssertionError(
                    "follow process was not terminated after downstream pipe closed"
                )
            return -signal.SIGTERM

        def terminate(self) -> None:
            """Record graceful termination from the streaming runner."""
            self.terminated.set()

        def kill(self) -> None:
            """Record forced termination from the streaming runner."""
            self.killed = True
            self.terminated.set()

    processes: list[_FollowProcess] = []

    def _popen(_args: list[str], **kwargs: object) -> _FollowProcess:
        """Create a follow-process double with piped stdout and stderr."""
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        process = _FollowProcess()
        processes.append(process)
        return process

    peer_sink = _PeerSink()
    broken_sink = _BrokenFlushSink(peer_sink.write_started)
    blocked_join_timeout = 0.01
    blocked_join_counts: dict[int, int] = {}
    original_join = threading.Thread.join

    def _join_with_peer_release(
        thread: threading.Thread,
        timeout: float | None = None,
    ) -> None:
        """Release the peer write only when teardown attempts the extra join."""
        target = getattr(thread, "_target", None)  # noqa: SLF001
        if (
            target is logs_mod._stream_redacted_pipe  # noqa: SLF001
            and thread.is_alive()
            and peer_sink.write_started.is_set()
            and timeout == blocked_join_timeout
        ):
            thread_key = id(thread)
            blocked_join_counts[thread_key] = blocked_join_counts.get(thread_key, 0) + 1
            if blocked_join_counts[thread_key] >= 2:
                peer_sink.release_write.set()
        return original_join(thread, timeout=timeout)

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(sys, "stdout", broken_sink)
    monkeypatch.setattr(sys, "stderr", peer_sink)
    monkeypatch.setattr(threading.Thread, "join", _join_with_peer_release)
    monkeypatch.setattr(
        logs_mod,
        "_STREAMING_BLOCKED_THREAD_JOIN_TIMEOUT_SECONDS",
        blocked_join_timeout,
    )
    monkeypatch.setattr(
        logs_mod,
        "_STREAMING_DOWNSTREAM_WRITE_POLL_SECONDS",
        0.001,
    )

    try:
        result = run_service_logs(
            services=[ServiceLogName.api],
            follow=True,
        )

        assert result == ServiceLogsResult(stdout="", stderr="")
        assert peer_sink.write_finished.is_set()
        assert any(count >= 2 for count in blocked_join_counts.values())
        assert len(processes) == 1
        assert processes[0].terminated.is_set()
        assert processes[0].killed is False
    finally:
        peer_sink.release_write.set()


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_non_follow_keyboard_interrupt_propagates() -> None:
    def _run(_args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_service_logs(
            services=[ServiceLogName.api],
            follow=False,
            run_subprocess=_run,
        )


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
@pytest.mark.parametrize(
    ("raised", "returncode", "detail"),
    [
        (FileNotFoundError("docker"), 127, "docker binary not found on PATH"),
        (OSError("permission denied"), 1, "OSError: permission denied"),
    ],
)
def test_service_logs_subprocess_start_errors_become_structured_failures(
    raised: Exception,
    returncode: int,
    detail: str,
) -> None:
    def _run(_args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise raised

    with pytest.raises(ServiceLogsError) as exc_info:
        run_service_logs(services=[ServiceLogName.api], run_subprocess=_run)

    assert exc_info.value.returncode == returncode
    assert exc_info.value.detail == detail


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_failure_prefers_stderr_then_stdout_then_generic_detail() -> None:
    def stderr_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=2, stdout="stdout", stderr="stderr")

    def stdout_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=3, stdout="stdout", stderr="")

    def empty_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=4, stdout="", stderr="")

    with pytest.raises(ServiceLogsError) as stderr_error:
        run_service_logs(services=[ServiceLogName.api], run_subprocess=stderr_run)
    with pytest.raises(ServiceLogsError) as stdout_error:
        run_service_logs(services=[ServiceLogName.api], run_subprocess=stdout_run)
    with pytest.raises(ServiceLogsError) as empty_error:
        run_service_logs(services=[ServiceLogName.api], run_subprocess=empty_run)

    assert stderr_error.value.detail == "stderr"
    assert stdout_error.value.detail == "stdout"
    assert empty_error.value.detail == "docker compose returned a non-zero exit status"


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_redacts_captured_output_and_failure_detail() -> None:
    """Redact captured service-log output and command failure details."""
    token = "ghp_serviceLogsSecret123456"
    plain_ref = "plain-file:///home/user/.awf/secrets/github.default"
    env_ref = "env://OPENAI_API_KEY"

    def success_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return successful compose logs that contain setup secret material."""
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout=f"stdout token={token} ref={plain_ref}",
            stderr=f"stderr credential_ref={env_ref}",
        )

    result = run_service_logs(services=[ServiceLogName.api], run_subprocess=success_run)

    rendered_success = result.stdout + result.stderr
    for raw in (token, plain_ref, env_ref, "/home/user/.awf/secrets/github.default"):
        assert raw not in rendered_success
    assert "<redacted>" in rendered_success

    def failure_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return a failing compose run whose stderr includes setup secret material."""
        return subprocess.CompletedProcess(
            args,
            returncode=2,
            stdout="",
            stderr=f"provider token={token} ref={plain_ref}",
        )

    with pytest.raises(ServiceLogsError) as exc_info:
        run_service_logs(services=[ServiceLogName.api], run_subprocess=failure_run)

    assert exc_info.value.returncode == 2
    for raw in (token, plain_ref, "/home/user/.awf/secrets/github.default"):
        assert raw not in exc_info.value.detail
    assert "<redacted>" in exc_info.value.detail


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_redacts_compose_env_provider_secret_from_captured_output(
    tmp_path: Path,
) -> None:
    """Redact selected Compose env provider credentials even when logs emit bare values."""
    secret = "compose-only-anthropic-auth-secret"
    override_secret = "host-override-anthropic-auth-secret"
    visible_value = "visible-compose-project"
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        (f"ANTHROPIC_AUTH_TOKEN={secret}\nCOMPOSE_PROJECT_NAME={visible_value}\n"),
        encoding="utf-8",
    )

    def success_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return captured output containing provider secrets and visible text."""
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout=f"stdout bare {secret}, {override_secret}, and {visible_value}\n",
            stderr=f"stderr bare {secret} and {override_secret}\n",
        )

    result = run_service_logs(
        services=[ServiceLogName.api],
        compose_env_file=compose_env_file,
        service_environ={"ANTHROPIC_AUTH_TOKEN": override_secret},
        run_subprocess=success_run,
    )

    rendered = result.stdout + result.stderr
    for raw in (secret, override_secret):
        assert raw not in rendered
    assert visible_value in rendered
    assert "<redacted>" in rendered


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_redacts_inherited_env_secret_from_captured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redact secret-like caller env values inherited by the default logs subprocess."""
    secret = "opaque-inherited-claude-value"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", secret)

    def success_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return captured output containing an inherited provider secret value."""
        assert kwargs["env"] is None
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout=f"stdout bare {secret}\n",
            stderr=f"stderr bare {secret}\n",
        )

    result = run_service_logs(
        services=[ServiceLogName.api],
        run_subprocess=success_run,
    )

    rendered = result.stdout + result.stderr
    assert secret not in rendered
    assert "<redacted>" in rendered


@pytest.mark.unit
def test_service_log_secret_values_skips_short_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep short exact-secret candidates out of service-log redaction inputs."""
    compose_short_secret = "a1!"
    compose_long_secret = "compose-secret"
    inherited_short_secret = "b2!"
    inherited_long_secret = "inherited-secret"
    explicit_short_secret = "c3!"
    explicit_long_secret = "explicit-secret"
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        (f"CUSTOM_API_KEY={compose_short_secret}\nCUSTOM_CLIENT_SECRET={compose_long_secret}\n"),
        encoding="utf-8",
    )
    monkeypatch.setenv("CUSTOM_AUTH_TOKEN", inherited_short_secret)
    monkeypatch.setenv("CUSTOM_LONG_AUTH_TOKEN", inherited_long_secret)

    values = _service_log_secret_values(
        {
            "CUSTOM_PASSWORD": explicit_short_secret,
            "CUSTOM_LONG_PASSWORD": explicit_long_secret,
        },
        compose_env_file,
    )

    selected_short_values = [
        raw
        for raw in (compose_short_secret, inherited_short_secret, explicit_short_secret)
        if raw in values
    ]
    missing_long_values = [
        raw
        for raw in (compose_long_secret, inherited_long_secret, explicit_long_secret)
        if raw not in values
    ]
    assert not selected_short_values
    assert not missing_long_values


@pytest.mark.unit
def test_service_log_secret_values_resolves_omitted_compose_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resolve the public omitted env-file sentinel before parsing env files."""
    secret = "compose-sentinel-secret"
    compose_env_file = tmp_path / LOCAL_SERVICE_COMPOSE_ENV_FILE
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text(f"CUSTOM_API_KEY={secret}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    values = _service_log_secret_values({}, COMPOSE_ENV_FILE_OMITTED)

    assert secret in values


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_resolves_omitted_compose_env_file_before_subprocess(
    tmp_path: Path,
) -> None:
    """Keep the public omitted env-file sentinel out of subprocess inputs."""
    secret = "compose-sentinel-log-secret"
    compose_env_file = tmp_path / LOCAL_SERVICE_COMPOSE_ENV_FILE
    compose_env_file.parent.mkdir(parents=True, exist_ok=True)
    compose_env_file.write_text(f"CUSTOM_API_KEY={secret}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def success_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Record the subprocess arguments and return redaction input."""
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout=f"{secret}\n", stderr="")

    result = run_service_logs(
        services=[ServiceLogName.api],
        compose_env_file=COMPOSE_ENV_FILE_OMITTED,
        run_subprocess=success_run,
    )

    assert calls[0][2:4] == ["--env-file", str(LOCAL_SERVICE_COMPOSE_ENV_FILE)]
    assert secret not in result.stdout
    assert "<redacted>" in result.stdout


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_redacts_compose_env_provider_secret_from_streamed_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Apply selected Compose env exact-secret redaction to followed log streams."""
    secret = "compose-only-claude-auth-secret"
    visible_value = "visible-stream-project"
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        (f"ANTHROPIC_AUTH_TOKEN={secret}\nCOMPOSE_PROJECT_NAME={visible_value}\n"),
        encoding="utf-8",
    )

    class _FollowProcess:
        """Follow process double that streams provider secrets."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """Expose stdout and stderr streams containing secret-bearing lines."""
            self.stdout = io.StringIO(f"stdout bare {secret} and {visible_value}\n")
            self.stderr = io.StringIO(f"stderr bare {secret}\n")

        def wait(self, timeout: float | None = None) -> int:
            """Finish immediately after the streaming threads read both pipes."""
            assert timeout is None
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FollowProcess)

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
        compose_env_file=compose_env_file,
    )

    captured = capfd.readouterr()
    rendered = captured.out + captured.err
    assert result == ServiceLogsResult(stdout="", stderr="")
    assert secret not in rendered
    assert visible_value in rendered
    assert "<redacted>" in rendered


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_redacts_multiline_compose_env_secret_from_streamed_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Keep multiline exact secrets from leaking across followed log lines."""
    secret = "line-one-compose-auth-secret\nline-two-compose-auth-secret"
    visible_value = "visible-stream-project"
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        (f'ANTHROPIC_AUTH_TOKEN="{secret}"\nCOMPOSE_PROJECT_NAME={visible_value}\n'),
        encoding="utf-8",
    )

    class _FollowProcess:
        """Follow process double that streams a multiline provider secret."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """Expose streams where no single line contains the full secret."""
            self.stdout = io.StringIO(f"stdout bare {secret}\nstdout {visible_value}\n")
            self.stderr = io.StringIO(f"stderr bare {secret}\n")

        def wait(self, timeout: float | None = None) -> int:
            """Finish immediately after the streaming threads read both pipes."""
            assert timeout is None
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FollowProcess)

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
        compose_env_file=compose_env_file,
    )

    captured = capfd.readouterr()
    rendered = captured.out + captured.err
    assert result == ServiceLogsResult(stdout="", stderr="")
    for fragment in secret.splitlines():
        assert fragment not in rendered
    assert visible_value in rendered
    assert "<redacted>" in rendered


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_redacts_overlapping_multiline_secret_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Do not split an exact secret while holding context for another one."""
    first_secret = "alpha-compose-secret\nbeta-compose-secret"
    second_secret = "beta-compose-secret\ngamma-compose-secret"
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        (f'ANTHROPIC_AUTH_TOKEN="{first_secret}"\nCUSTOM_CLIENT_SECRET="{second_secret}"\n'),
        encoding="utf-8",
    )

    class _FollowProcess:
        """Follow process double with overlapping multiline secret candidates."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """Expose a stream where the overlap appears at a line boundary."""
            self.stdout = io.StringIO(
                "stdout alpha-compose-secret\nbeta-compose-secret\ngamma-compose-secret\n"
            )
            self.stderr = io.StringIO("")

        def wait(self, timeout: float | None = None) -> int:
            """Finish immediately after the streaming threads read both pipes."""
            assert timeout is None
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FollowProcess)

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
        compose_env_file=compose_env_file,
    )

    rendered = capfd.readouterr().out
    assert result == ServiceLogsResult(stdout="", stderr="")
    for fragment in ("alpha-compose-secret", "beta-compose-secret", "gamma-compose-secret"):
        assert fragment not in rendered
    assert "<redacted>" in rendered


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_flushes_multiline_secret_prefix_at_eof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Do not drop ordinary output that only looked like a partial secret."""
    secret = "prefix-compose-secret\nsuffix-compose-secret"
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        f'ANTHROPIC_AUTH_TOKEN="{secret}"\n',
        encoding="utf-8",
    )

    class _FollowProcess:
        """Follow process double ending after a possible secret prefix."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """Expose a stream that ends before the configured secret completes."""
            self.stdout = io.StringIO("stdout prefix-compose-secret\n")
            self.stderr = io.StringIO("")

        def wait(self, timeout: float | None = None) -> int:
            """Finish immediately after the streaming threads read both pipes."""
            assert timeout is None
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FollowProcess)

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
        compose_env_file=compose_env_file,
    )

    rendered = capfd.readouterr().out
    assert result == ServiceLogsResult(stdout="", stderr="")
    assert rendered == "stdout prefix-compose-secret\n"


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_redacts_inherited_env_secret_from_streamed_output(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Apply inherited exact-secret redaction to followed log streams."""
    secret = "opaque-inherited-follow-claude-value"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", secret)

    class _FollowProcess:
        """Follow process double that streams inherited provider secrets."""

        def __init__(self, *_args: object, **kwargs: object) -> None:
            """Expose stdout and stderr streams containing secret-bearing lines."""
            assert kwargs["env"] is None
            self.stdout = io.StringIO(f"stdout bare {secret}\n")
            self.stderr = io.StringIO(f"stderr bare {secret}\n")

        def wait(self, timeout: float | None = None) -> int:
            """Finish immediately after the streaming threads read both pipes."""
            assert timeout is None
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FollowProcess)

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
    )

    captured = capfd.readouterr()
    rendered = captured.out + captured.err
    assert result == ServiceLogsResult(stdout="", stderr="")
    assert secret not in rendered
    assert "<redacted>" in rendered


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_follow_success_discards_uncaptured_output() -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["capture_output"] is False
        return subprocess.CompletedProcess(args, returncode=0, stdout=None, stderr=None)

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
        run_subprocess=_run,
    )

    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.unit
def test_service_logs_default_subprocess_runner_executes_command() -> None:
    """Exercise the default captured subprocess runner with a tiny command."""
    result = _run_subprocess(
        [sys.executable, "-c", "print('logs-ok')"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout is not None
    assert result.stdout.strip() == "logs-ok"


@pytest.mark.unit
def test_service_logs_default_follow_runner_redacts_streamed_output(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Exercise streaming redaction in the default follow subprocess runner."""
    token = "ghp_serviceLogsSecret123456"
    plain_ref = "plain-file:///home/user/.awf/secrets/github.default"
    script = (
        "import sys; "
        f"print('stdout token={token}'); "
        f"print('stderr ref={plain_ref}', file=sys.stderr)"
    )

    result = _run_subprocess(
        [sys.executable, "-c", script],
        check=False,
        capture_output=False,
        text=True,
    )

    captured = capfd.readouterr()
    rendered = captured.out + captured.err
    assert result.returncode == 0
    assert result.stdout is None
    assert result.stderr is None
    for raw in (token, plain_ref, "/home/user/.awf/secrets/github.default"):
        assert raw not in rendered
    assert "<redacted>" in captured.out
    assert "<redacted>" in captured.err


@pytest.mark.unit
def test_service_logs_default_follow_runner_replaces_invalid_bytes(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Keep followed streams alive when container logs contain non-UTF-8 bytes."""
    script = (
        "import sys; "
        "sys.stdout.buffer.write(b'stdout before-\\xff-after\\n'); "
        "sys.stdout.flush(); "
        "sys.stderr.buffer.write(b'stderr before-\\xfe-after\\n'); "
        "sys.stderr.flush()"
    )

    result = _run_subprocess(
        [sys.executable, "-c", script],
        check=False,
        capture_output=False,
        text=True,
    )

    replacement = "\N{REPLACEMENT CHARACTER}"
    captured = capfd.readouterr()
    assert result.returncode == 0
    assert f"stdout before-{replacement}-after" in captured.out
    assert f"stderr before-{replacement}-after" in captured.err


@pytest.mark.unit
def test_service_logs_finds_default_compose_file_from_parent_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}")
    nested_dir = tmp_path / "nested" / "project"
    nested_dir.mkdir(parents=True)
    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.chdir(nested_dir)
    run_service_logs(services=[ServiceLogName.api], run_subprocess=_run)

    assert calls == [
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "logs",
            "--tail",
            str(DEFAULT_LOG_TAIL),
            "api",
        ]
    ]


@pytest.mark.unit
def test_service_logs_defaults_to_relative_compose_path_in_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}")
    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.chdir(tmp_path)
    run_service_logs(services=[ServiceLogName.api], run_subprocess=_run)

    assert calls == [
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "logs",
            "--tail",
            str(DEFAULT_LOG_TAIL),
            "api",
        ]
    ]


@pytest.mark.unit
def test_service_logs_default_file_missing_returns_scoped_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ServiceLogsError) as exc_info:
        run_service_logs(services=[ServiceLogName.api])

    assert exc_info.value.returncode == 1
    assert "Run awf service logs from an AWF source checkout" in exc_info.value.detail


@pytest.mark.unit
def test_resolve_local_service_compose_file_stops_at_home_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    repo_nested = repo_root / "nested"
    repo_nested.mkdir(parents=True)
    outside_home = tmp_path.parent / "outside-home"
    compose_file = outside_home / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}")

    monkeypatch.setattr("awf.service.logs.Path.home", lambda: tmp_path)
    monkeypatch.chdir(repo_nested)

    assert (
        _resolve_local_service_compose_file(LOCAL_SERVICE_COMPOSE_FILE)
        == LOCAL_SERVICE_COMPOSE_FILE
    )
