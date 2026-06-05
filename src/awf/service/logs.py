"""Read-only local service log helpers."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Literal, Protocol

import yaml

from awf.common.redaction import redact_secrets
from awf.common.token_patterns import compile_token_assignment_re
from awf.service.config import (
    COMPOSE_ENV_FILE_OMITTED,
    LOCAL_SERVICE_COMPOSE_ENV_FILE,
    LOCAL_SERVICE_COMPOSE_FILE,
    ComposeEnvFileInput,
    ComposeEnvFileOmitted,
)
from awf.service.environment import (
    cleared_docker_cli_client_keys,
    compose_cli_environ,
    compose_env_file_quoted_multiline_values,
    compose_env_file_values,
    compose_interpolation_environ,
    docker_cli_client_environ,
    env_lookup,
    non_empty_env_value,
)
from awf.service.provider_readiness import is_secret_env_key

DEFAULT_LOG_TAIL = 100
DEFAULT_LOG_SERVICES = ("api", "worker")
_FOLLOW_INTERRUPT_RETURN_CODES = {128 + signal.SIGINT, -signal.SIGINT}
_STREAMING_INTERRUPT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_STREAMING_DOWNSTREAM_WRITE_TIMEOUT_SECONDS = 5.0
_STREAMING_DOWNSTREAM_WRITE_POLL_SECONDS = 0.05
_STREAMING_BLOCKED_THREAD_JOIN_TIMEOUT_SECONDS = 0.05
_LOCAL_SERVICE_PROJECT_NAME = "awf-local-service"
_TOKEN_ASSIGNMENT_RE = compile_token_assignment_re()
_TOKEN_ASSIGNMENT_KEY_PATTERN = (
    r"(?:[A-Za-z][A-Za-z0-9_]*_)?TOKEN"
    r"|(?:[A-Za-z][A-Za-z0-9_]*_)?(?:API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY)"
    r"|(?:AUTH|GITHUB|GH)[_-]?TOKEN"
    r"|PASSWORD|PASSWD|SECRET"
)
_MULTILINE_PEM_ASSIGNMENT_START_RE = re.compile(
    rf"\b(?:{_TOKEN_ASSIGNMENT_KEY_PATTERN})\b"
    r"\s*[:=]\s*"
    r"(?:[\"'])?"
    r"\s*-----BEGIN [A-Z0-9 -]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_PENDING_PEM_ASSIGNMENT_PREFIX_RE = re.compile(
    rf"\b(?:{_TOKEN_ASSIGNMENT_KEY_PATTERN})\b"
    r"\s*[:=]\s*"
    r"(?:[\"'])?"
    r"\s*$",
    re.IGNORECASE,
)
_PEM_FOOTER_RE = re.compile(r"-----END [A-Z0-9 -]*PRIVATE KEY-----", re.IGNORECASE)


def _resolve_local_service_compose_file(compose_file: Path) -> Path:
    if compose_file != LOCAL_SERVICE_COMPOSE_FILE:
        return compose_file
    if compose_file.exists():
        return compose_file.resolve()
    home = Path.home()
    for root in Path.cwd().parents:
        candidate = root / compose_file
        if candidate.exists():
            return candidate.resolve()
        if root == home:
            break
    return compose_file


def _local_service_compose_not_found_message(compose_file: Path) -> str:
    return (
        f"Cannot resolve service compose file '{compose_file}'. "
        "Run awf service logs from an AWF source checkout (where "
        f"'{compose_file}' exists) or use container-level Docker logs for that project "
        f"({_LOCAL_SERVICE_PROJECT_NAME}) when running an installed package."
    )


class ServiceLogName(StrEnum):
    api = "api"
    worker = "worker"
    migrate = "migrate"
    postgres = "postgres"


class CompletedProcessLike(Protocol):
    @property
    def returncode(self) -> int: ...  # pragma: no cover

    @property
    def stdout(self) -> str | None: ...  # pragma: no cover

    @property
    def stderr(self) -> str | None: ...  # pragma: no cover


class SubprocessRun(Protocol):
    """Callable protocol for invoking Docker log subprocess commands."""

    def __call__(
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        env: Mapping[str, str] | None = None,
    ) -> CompletedProcessLike:
        """Run a logs command and return a completed-process-like object."""
        ...  # pragma: no cover


@dataclass(frozen=True)
class ServiceLogsResult:
    stdout: str
    stderr: str


class ServiceLogsError(RuntimeError):
    def __init__(self, *, returncode: int, detail: str) -> None:
        super().__init__(detail)
        self.returncode = returncode
        self.detail = detail


def service_logs_command(
    *,
    services: Sequence[ServiceLogName],
    tail: int = DEFAULT_LOG_TAIL,
    follow: bool = False,
    compose_file: Path = LOCAL_SERVICE_COMPOSE_FILE,
    compose_env_file: Path | None = None,
) -> list[str]:
    """Build the Docker Compose logs command for the selected services."""

    selected_services = [service.value for service in services] or list(DEFAULT_LOG_SERVICES)
    args = [
        "docker",
        "compose",
    ]
    if compose_env_file is not None:
        args.extend(["--env-file", str(compose_env_file)])
    args.extend(["-f", str(compose_file), "logs", "--tail", str(tail)])
    if follow:
        args.append("--follow")
    args.extend(selected_services)
    return args


def run_service_logs(
    *,
    services: Sequence[ServiceLogName],
    tail: int = DEFAULT_LOG_TAIL,
    follow: bool = False,
    compose_file: Path = LOCAL_SERVICE_COMPOSE_FILE,
    compose_env_file: ComposeEnvFileInput = COMPOSE_ENV_FILE_OMITTED,
    service_environ: Mapping[str, str] | None = None,
    run_subprocess: SubprocessRun | None = None,
) -> ServiceLogsResult:
    """Run ``docker compose logs`` for the local service stack."""

    capture_output = not follow
    discover_default_compose_env_file = compose_file == LOCAL_SERVICE_COMPOSE_FILE
    compose_file = _resolve_local_service_compose_file(compose_file)
    if compose_file == LOCAL_SERVICE_COMPOSE_FILE and not compose_file.exists():
        raise ServiceLogsError(
            returncode=1, detail=_local_service_compose_not_found_message(compose_file)
        )
    resolved_compose_env_file = _resolve_service_log_compose_env_file(
        compose_env_file,
        compose_file=compose_file,
        discover_default_compose_env_file=discover_default_compose_env_file,
    )
    extra_secrets = _service_log_secret_values(service_environ, resolved_compose_env_file)
    if run_subprocess is None:

        def runner(
            args: list[str],
            *,
            check: bool,
            capture_output: bool,
            text: Literal[True],
            env: Mapping[str, str] | None = None,
        ) -> CompletedProcessLike:
            """Run logs with the collected exact-secret redaction context."""
            return _run_subprocess(
                args,
                check=check,
                capture_output=capture_output,
                text=text,
                env=env,
                extra_secrets=extra_secrets,
            )

    else:
        runner = run_subprocess
    try:
        docker_env = _docker_cli_environ(
            service_environ,
            compose_file=compose_file,
            compose_env_file=resolved_compose_env_file,
        )
    except yaml.YAMLError as exc:
        raise ServiceLogsError(
            returncode=1,
            detail=redact_secrets(str(exc), extra_secrets=extra_secrets),
        ) from exc
    command = service_logs_command(
        services=services,
        tail=tail,
        follow=follow,
        compose_file=compose_file,
        compose_env_file=resolved_compose_env_file,
    )
    try:
        result = runner(
            command,
            check=False,
            capture_output=capture_output,
            text=True,
            env=docker_env,
        )
    except FileNotFoundError as exc:
        raise ServiceLogsError(returncode=127, detail="docker binary not found on PATH") from exc
    except OSError as exc:
        detail = redact_secrets(f"{type(exc).__name__}: {exc}", extra_secrets=extra_secrets)
        raise ServiceLogsError(returncode=1, detail=detail) from exc
    except KeyboardInterrupt:
        if follow:
            return ServiceLogsResult(stdout="", stderr="")
        raise

    stdout = redact_secrets(result.stdout or "", extra_secrets=extra_secrets)
    stderr = redact_secrets(result.stderr or "", extra_secrets=extra_secrets)
    if follow and result.returncode in _FOLLOW_INTERRUPT_RETURN_CODES:
        return ServiceLogsResult(stdout="", stderr="")
    if result.returncode != 0:
        raise ServiceLogsError(
            returncode=result.returncode,
            detail=_failure_detail(stdout=stdout, stderr=stderr, follow=follow),
        )
    if follow:
        return ServiceLogsResult(stdout="", stderr="")
    return ServiceLogsResult(stdout=stdout, stderr=stderr)


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    env: Mapping[str, str] | None = None,
    extra_secrets: Iterable[str] = (),
) -> CompletedProcessLike:
    """Run the logs subprocess, omitting env when no override is needed."""

    if not capture_output:
        return _run_streaming_subprocess(
            args,
            check=check,
            text=text,
            env=env,
            extra_secrets=extra_secrets,
        )
    if env is None:
        return subprocess.run(args, check=check, capture_output=capture_output, text=text)
    return subprocess.run(args, check=check, capture_output=capture_output, text=text, env=env)


def _run_streaming_subprocess(
    args: list[str],
    *,
    check: bool,
    text: Literal[True],
    env: Mapping[str, str] | None = None,
    extra_secrets: Iterable[str] = (),
) -> CompletedProcessLike:
    """Run a subprocess while streaming redacted stdout and stderr."""
    extra_secret_values = tuple(extra_secrets)
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        encoding="utf-8",
        errors="replace",
        env=env,
    )

    stream_broken_pipe = threading.Event()
    stream_broken_pipe_lock = threading.Lock()
    stream_write_lock = threading.Lock()
    active_stream_writes: dict[int, float] = {}
    stream_watch_stop = threading.Event()

    def _handle_stream_broken_pipe() -> None:
        """Stop the streaming child after the downstream pipe closes."""
        with stream_broken_pipe_lock:
            if stream_broken_pipe.is_set():
                return
            stream_broken_pipe.set()
            _terminate_streaming_subprocess(process)

    def _mark_stream_write_start() -> None:
        """Record a stream thread entering a downstream write."""
        with stream_write_lock:
            active_stream_writes[threading.get_ident()] = time.monotonic()

    def _mark_stream_write_end() -> None:
        """Clear a stream thread's active downstream write marker."""
        with stream_write_lock:
            active_stream_writes.pop(threading.get_ident(), None)

    def _has_blocked_downstream_write() -> bool:
        """Return whether any stream write has exceeded the blocked-write timeout."""
        now = time.monotonic()
        with stream_write_lock:
            return any(
                now - started_at >= _STREAMING_DOWNSTREAM_WRITE_TIMEOUT_SECONDS
                for started_at in active_stream_writes.values()
            )

    def _watch_blocked_downstream_writes() -> None:
        """Terminate the followed child if a downstream log sink stops draining."""
        while not stream_watch_stop.wait(_STREAMING_DOWNSTREAM_WRITE_POLL_SECONDS):
            if stream_broken_pipe.is_set():
                return
            if _has_blocked_downstream_write():
                _handle_stream_broken_pipe()
                return

    def _join_stream_threads(threads: tuple[threading.Thread, ...]) -> None:
        """Join stream threads without hanging behind an already-blocked sink write."""
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=_STREAMING_DOWNSTREAM_WRITE_POLL_SECONDS)
            if stream_broken_pipe.is_set():
                for thread in threads:
                    thread.join(timeout=_STREAMING_BLOCKED_THREAD_JOIN_TIMEOUT_SECONDS)
                return

    stdout_thread = _start_redacted_stream_thread(
        process.stdout,
        sys.stdout,
        extra_secrets=extra_secret_values,
        on_broken_pipe=_handle_stream_broken_pipe,
        on_write_start=_mark_stream_write_start,
        on_write_end=_mark_stream_write_end,
    )
    stderr_thread = _start_redacted_stream_thread(
        process.stderr,
        sys.stderr,
        extra_secrets=extra_secret_values,
        on_broken_pipe=_handle_stream_broken_pipe,
        on_write_start=_mark_stream_write_start,
        on_write_end=_mark_stream_write_end,
    )
    watchdog_thread = threading.Thread(
        target=_watch_blocked_downstream_writes,
        daemon=True,
    )
    watchdog_thread.start()
    stream_threads = (stdout_thread, stderr_thread)
    try:
        returncode = process.wait()
    except KeyboardInterrupt:
        _terminate_streaming_subprocess(process)
        raise
    finally:
        _join_stream_threads(stream_threads)
        stream_watch_stop.set()
        watchdog_thread.join(
            timeout=(
                _STREAMING_BLOCKED_THREAD_JOIN_TIMEOUT_SECONDS
                if stream_broken_pipe.is_set()
                else None
            )
        )
        if stream_broken_pipe.is_set():
            for thread in stream_threads:
                thread.join(timeout=_STREAMING_BLOCKED_THREAD_JOIN_TIMEOUT_SECONDS)
    if stream_broken_pipe.is_set():
        return subprocess.CompletedProcess(args, 0, stdout=None, stderr=None)
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, args)
    return subprocess.CompletedProcess(args, returncode, stdout=None, stderr=None)


def _terminate_streaming_subprocess(process: subprocess.Popen[str]) -> None:
    """Terminate and reap a streaming child after an operator interrupt."""
    process.terminate()
    try:
        process.wait(timeout=_STREAMING_INTERRUPT_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _start_redacted_stream_thread(
    source: IO[str] | None,
    sink: IO[str],
    *,
    extra_secrets: Iterable[str] = (),
    on_broken_pipe: Callable[[], None],
    on_write_start: Callable[[], None],
    on_write_end: Callable[[], None],
) -> threading.Thread:
    """Start a thread that redacts a subprocess pipe before writing it."""
    thread = threading.Thread(
        target=_stream_redacted_pipe,
        args=(
            source,
            sink,
            tuple(extra_secrets),
            on_broken_pipe,
            on_write_start,
            on_write_end,
        ),
        daemon=True,
    )
    thread.start()
    return thread


def _stream_redacted_pipe(
    source: IO[str] | None,
    sink: IO[str],
    extra_secrets: Iterable[str],
    on_broken_pipe: Callable[[], None],
    on_write_start: Callable[[], None],
    on_write_end: Callable[[], None],
) -> None:
    """Copy pipe lines to a sink after applying shared secret redaction."""
    if source is None:
        return
    extra_secret_values = tuple(extra_secrets)
    multiline_secret_values = _multiline_exact_secret_values(extra_secret_values)
    pending = ""

    def write_redacted_chunk(chunk: str) -> bool:
        """Write one already-redacted stream chunk, handling downstream closure."""
        try:
            on_write_start()
            try:
                sink.write(chunk)
                sink.flush()
            finally:
                on_write_end()
        except (OSError, ValueError):
            try:
                source.close()
            finally:
                on_broken_pipe()
            return False
        return True

    try:
        for line in source:
            pending += line
            flush_length = _stream_redaction_flushable_length(
                pending,
                multiline_secret_values,
            )
            if flush_length <= 0:
                continue
            chunk = pending[:flush_length]
            pending = pending[flush_length:]
            redacted_chunk = redact_secrets(chunk, extra_secrets=extra_secret_values)
            if not write_redacted_chunk(redacted_chunk):
                return
        if pending:
            redacted_chunk = redact_secrets(pending, extra_secrets=extra_secret_values)
            if not write_redacted_chunk(redacted_chunk):
                return
    except BrokenPipeError:
        try:
            source.close()
        finally:
            on_broken_pipe()


def _multiline_exact_secret_values(extra_secrets: Iterable[str]) -> tuple[str, ...]:
    """Return exact secret values that can cross followed stream line boundaries."""
    return tuple(
        dict.fromkeys(
            secret
            for secret in extra_secrets
            if len(secret) >= 4 and ("\n" in secret or "\r" in secret)
        )
    )


def _stream_redaction_flushable_length(text: str, multiline_secrets: Sequence[str]) -> int:
    """Return the prefix length safe to redact and write from a pending stream."""
    held_suffix_length = max(
        _pending_multiline_secret_prefix_length(text, multiline_secrets),
        _pending_pem_assignment_prefix_length(text),
    )
    flush_length = len(text) - held_suffix_length
    spans = _multiline_exact_secret_spans(text, multiline_secrets)
    unclosed_pem_assignment_start = _unclosed_multiline_pem_assignment_start(text)
    if unclosed_pem_assignment_start is not None:
        spans.append((unclosed_pem_assignment_start, len(text) + 1))
    while flush_length > 0:
        next_flush_length = flush_length
        for start, end in spans:
            if start < next_flush_length < end:
                next_flush_length = min(next_flush_length, start)
        if next_flush_length == flush_length:
            return flush_length
        flush_length = next_flush_length
    return 0


def _pending_pem_assignment_prefix_length(text: str) -> int:
    """Return a held suffix that may become a multiline PEM assignment."""
    match = _PENDING_PEM_ASSIGNMENT_PREFIX_RE.search(text)
    if match is None:
        return 0
    return len(text) - match.start()


def _unclosed_multiline_pem_assignment_start(text: str) -> int | None:
    """Return the first PEM assignment start that needs more stream context."""
    complete_spans = _complete_multiline_pem_assignment_spans(text)
    for match in _MULTILINE_PEM_ASSIGNMENT_START_RE.finditer(text):
        start = match.start()
        if not any(span_start <= start < span_end for span_start, span_end in complete_spans):
            return start
    return None


def _complete_multiline_pem_assignment_spans(text: str) -> list[tuple[int, int]]:
    """Find complete PEM assignment spans matched by the shared redactor pattern."""
    if "-----BEGIN" not in text or "PRIVATE KEY-----" not in text:
        return []
    spans: list[tuple[int, int]] = []
    for match in _TOKEN_ASSIGNMENT_RE.finditer(text):
        value = match.group("value")
        if value.startswith("-----BEGIN") and _PEM_FOOTER_RE.search(value):
            spans.append(match.span())
    return spans


def _pending_multiline_secret_prefix_length(text: str, multiline_secrets: Sequence[str]) -> int:
    """Return the longest text suffix that could become a multiline exact secret."""
    held_suffix_length = 0
    for secret in multiline_secrets:
        max_prefix_length = min(len(text), len(secret) - 1)
        for prefix_length in range(max_prefix_length, held_suffix_length, -1):
            if text.endswith(secret[:prefix_length]):
                held_suffix_length = prefix_length
                break
    return held_suffix_length


def _multiline_exact_secret_spans(
    text: str,
    multiline_secrets: Sequence[str],
) -> list[tuple[int, int]]:
    """Find exact multiline secret spans in pending stream text."""
    spans: list[tuple[int, int]] = []
    for secret in sorted(multiline_secrets, key=len):
        cursor = 0
        while True:
            start = text.find(secret, cursor)
            if start == -1:
                break
            end = start + len(secret)
            spans.append((start, end))
            cursor = start + 1
    return spans


def _service_log_secret_values(
    environ: Mapping[str, str] | None,
    compose_env_file: Path | None,
) -> tuple[str, ...]:
    """Return exact service env values that service logs must redact."""
    (
        quoted_multiline_values,
        quoted_multiline_first_line_values,
    ) = _service_log_quoted_multiline_secret_context(compose_env_file)
    compose_environ = None if environ is None else {**os.environ, **environ}
    secret_values = [
        value
        for key, value in compose_env_file_values(compose_env_file, environ=compose_environ).items()
        if value
        and len(value) >= 4
        and is_secret_env_key(key)
        and (key, value) not in quoted_multiline_first_line_values
    ]
    secret_values.extend(quoted_multiline_values)
    for source_environ in (os.environ, environ):
        if source_environ is None:
            continue
        secret_values.extend(
            value
            for key, value in source_environ.items()
            if value and len(value) >= 4 and is_secret_env_key(key)
        )
    return tuple(dict.fromkeys(secret_values))


def _service_log_quoted_multiline_secret_context(
    compose_env_file: Path | None,
) -> tuple[tuple[str, ...], frozenset[tuple[str, str]]]:
    """Return full quoted multiline secrets and parsed first-line fragments."""
    values: list[str] = []
    first_line_values: set[tuple[str, str]] = set()
    for entry in compose_env_file_quoted_multiline_values(compose_env_file):
        if len(entry.value) < 4 or not is_secret_env_key(entry.key):
            continue
        values.append(entry.value)
        if not entry.closed_on_first_line:
            first_line_values.add((entry.key, entry.first_line_value))
    return tuple(values), frozenset(first_line_values)


def _resolve_service_log_compose_env_file(
    compose_env_file: ComposeEnvFileInput,
    *,
    compose_file: Path | None = None,
    discover_default_compose_env_file: bool = False,
) -> Path | None:
    """Resolve omitted service-log env-file input to the local default."""
    if isinstance(compose_env_file, ComposeEnvFileOmitted):
        if discover_default_compose_env_file and compose_file is not None:
            candidate = compose_file.parent / LOCAL_SERVICE_COMPOSE_ENV_FILE.name
            if candidate.exists():
                return candidate
            return None
        if compose_file is not None:
            return None
        return LOCAL_SERVICE_COMPOSE_ENV_FILE
    return compose_env_file


def _docker_cli_environ(
    environ: Mapping[str, str] | None,
    *,
    compose_file: Path,
    compose_env_file: Path | None,
) -> dict[str, str] | None:
    """Return the minimal subprocess env needed by Docker Compose logs."""

    if environ is None:
        return None
    awf_docker_host = non_empty_env_value(environ, "AWF_DOCKER_HOST")
    docker_host_found, docker_host_value = env_lookup(environ, "DOCKER_HOST")
    docker_host = awf_docker_host or (docker_host_value if docker_host_value else None)
    caller_docker_host_found, caller_docker_host_value = env_lookup(os.environ, "DOCKER_HOST")
    clears_docker_host = (
        docker_host_found
        and not docker_host_value
        and caller_docker_host_found
        and bool(caller_docker_host_value)
    )
    compose_env = compose_interpolation_environ(
        environ,
        compose_file=compose_file,
        compose_env_file=compose_env_file,
    )
    compose_cli_env = compose_cli_environ(environ)
    docker_cli_env = docker_cli_client_environ(environ)
    cleared_docker_cli_keys = cleared_docker_cli_client_keys(environ)
    if (
        not docker_host
        and not compose_env
        and not compose_cli_env
        and not docker_cli_env
        and not cleared_docker_cli_keys
        and not clears_docker_host
    ):
        # Compose reads ordinary service values through --env-file; only pass an
        # explicit subprocess environment when a resolved value must override the
        # caller environment for Docker client selection, Compose interpolation,
        # or Compose project/profile selection.
        return None
    resolved = dict(os.environ)
    resolved.update(docker_cli_env)
    resolved.update(compose_env)
    resolved.update(compose_cli_env)
    scrubbed_keys = {"AWF_DOCKER_HOST", *cleared_docker_cli_keys}
    if docker_host or clears_docker_host:
        scrubbed_keys.update({"DOCKER_CONTEXT", "DOCKER_HOST"})
    for key in list(resolved):
        if key.upper() in scrubbed_keys:
            del resolved[key]
    if docker_host:
        resolved["DOCKER_HOST"] = docker_host
    return resolved


def _failure_detail(*, stdout: str, stderr: str, follow: bool = False) -> str:
    detail = (stderr or stdout).strip()
    if detail:
        return detail
    if follow:
        return (
            "docker compose logs --follow exited with a non-zero status; "
            "docker output was already streamed to the terminal"
        )
    return "docker compose returned a non-zero exit status"
