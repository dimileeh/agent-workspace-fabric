"""Local service log helper tests."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

from awf.service.logs import (
    DEFAULT_LOG_TAIL,
    ServiceLogName,
    ServiceLogsError,
    _run_subprocess,
    run_service_logs,
    service_logs_command,
)


@pytest.mark.unit
def test_service_logs_command_defaults_and_follow_flag() -> None:
    command = service_logs_command(services=[], tail=25, compose_file=Path("compose.yml"))
    follow_command = service_logs_command(
        services=[ServiceLogName.postgres],
        tail=50,
        follow=True,
        compose_file=Path("compose.yml"),
    )

    assert command == [
        "docker",
        "compose",
        "-f",
        "compose.yml",
        "logs",
        "--tail",
        "25",
        "api",
        "worker",
    ]
    assert follow_command == [
        "docker",
        "compose",
        "-f",
        "compose.yml",
        "logs",
        "--tail",
        "50",
        "--follow",
        "postgres",
    ]


@pytest.mark.unit
def test_service_logs_returns_captured_output_for_non_follow_success() -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[-1] == "worker"
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(args, returncode=0, stdout="out", stderr="err")

    result = run_service_logs(services=[ServiceLogName.worker], run_subprocess=_run)

    assert result.stdout == "out"
    assert result.stderr == "err"


@pytest.mark.unit
def test_service_logs_follow_failure_mentions_terminal_output() -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs == {"check": False, "capture_output": False, "text": True}
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
        "docker output was already written directly to the terminal"
    )


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
            str(compose_file.relative_to(tmp_path)),
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
