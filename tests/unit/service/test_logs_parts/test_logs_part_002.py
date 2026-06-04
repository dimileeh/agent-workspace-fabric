"""Local service log helper tests."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from awf.service.logs import (
    DEFAULT_LOG_TAIL,
    LOCAL_SERVICE_COMPOSE_FILE,
    ServiceLogName,
    ServiceLogsError,
    _resolve_local_service_compose_file,
    _run_subprocess,
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
        "docker output was already written directly to the terminal"
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
