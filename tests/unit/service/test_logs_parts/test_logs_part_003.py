"""Local service log redaction tests split from part 002."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from awf.service.config import COMPOSE_ENV_FILE_OMITTED, LOCAL_SERVICE_COMPOSE_ENV_FILE
from awf.service.logs import (
    LOCAL_SERVICE_COMPOSE_FILE,
    ServiceLogName,
    ServiceLogsResult,
    _resolve_service_log_compose_env_file,
    _service_log_secret_values,
    run_service_logs,
)


@pytest.fixture
def _default_local_service_compose_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}")
    monkeypatch.chdir(tmp_path)


def _write_compose_file(tmp_path: Path, contents: str) -> Path:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(contents, encoding="utf-8")
    return compose_file


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


@pytest.mark.unit
def test_service_logs_redacts_compose_env_secret_interpolated_from_service_environ(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Parse env-file exact secrets with the same interpolation env passed to Compose."""
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      VALUE_SUFFIX: "${VALUE_SUFFIX:?set VALUE_SUFFIX}"
""",
    )
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        "ANTHROPIC_AUTH_TOKEN=provider-${VALUE_SUFFIX}\n",
        encoding="utf-8",
    )
    service_environ = {"VALUE_SUFFIX": "service-current-suffix"}
    stale_secret = "provider-caller-stale-suffix"
    current_secret = "provider-service-current-suffix"
    calls: list[dict[str, object]] = []

    def success_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return captured output containing the service-interpolated secret."""
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout=f"stdout bare {current_secret}\n",
            stderr="",
        )

    monkeypatch.setenv("VALUE_SUFFIX", "caller-stale-suffix")

    result = run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        compose_env_file=compose_env_file,
        service_environ=service_environ,
        run_subprocess=success_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["VALUE_SUFFIX"] == "service-current-suffix"
    assert current_secret not in result.stdout
    assert stale_secret not in result.stdout
    assert "<redacted>" in result.stdout


@pytest.mark.unit
def test_service_logs_redacts_double_quoted_multiline_secret_interpolated_from_service_environ(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Redact resolved physical multiline Compose env-file provider credentials."""
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      VALUE_SUFFIX: "${VALUE_SUFFIX:?set VALUE_SUFFIX}"
""",
    )
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        "\n".join(
            [
                'ANTHROPIC_AUTH_TOKEN="provider-${VALUE_SUFFIX}',
                'body-${VALUE_SUFFIX}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    service_environ = {"VALUE_SUFFIX": "service-current-suffix"}
    current_secret = "provider-service-current-suffix\nbody-service-current-suffix"
    stale_secret = "provider-caller-stale-suffix\nbody-caller-stale-suffix"

    def success_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return captured output containing the service-interpolated multiline secret."""
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout=f"stdout bare {current_secret}\n",
            stderr=f"stderr bare {current_secret}\n",
        )

    monkeypatch.setenv("VALUE_SUFFIX", "caller-stale-suffix")

    result = run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        compose_env_file=compose_env_file,
        service_environ=service_environ,
        run_subprocess=success_run,
    )

    rendered = result.stdout + result.stderr
    for fragment in current_secret.splitlines():
        assert fragment not in rendered
    assert stale_secret not in rendered
    assert "<redacted>" in rendered


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_redacts_single_quoted_multiline_compose_env_secret_from_captured_output(
    tmp_path: Path,
) -> None:
    """Redact every fragment of a Compose single-quoted multiline provider secret."""
    secret = "opaque-first-fragment\noperator's-second-fragment\nopaque-third-fragment"
    escaped_secret = secret.replace("'", "\\'")
    visible_value = "visible-compose-project"
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        (f"ANTHROPIC_AUTH_TOKEN='{escaped_secret}'\nCOMPOSE_PROJECT_NAME={visible_value}\n"),
        encoding="utf-8",
    )

    def success_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return captured output containing a bare multiline provider secret."""
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout=f"stdout bare {secret} and {visible_value}\n",
            stderr=f"stderr bare {secret}\n",
        )

    result = run_service_logs(
        services=[ServiceLogName.api],
        compose_env_file=compose_env_file,
        run_subprocess=success_run,
    )

    rendered = result.stdout + result.stderr
    for fragment in secret.splitlines():
        assert fragment not in rendered
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
def test_service_log_secret_values_excludes_multiline_first_line_fragment(
    tmp_path: Path,
) -> None:
    """Collect the full multiline secret without treating its first line as exact."""
    first_line = "prefix-compose-secret"
    secret = f"{first_line}\nsuffix-compose-secret"
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        f'ANTHROPIC_AUTH_TOKEN="{secret}"\n',
        encoding="utf-8",
    )

    values = _service_log_secret_values({}, compose_env_file)

    assert secret in values
    assert first_line not in values


@pytest.mark.unit
def test_service_log_secret_values_reads_resolved_omitted_compose_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Read provider secrets after resolving the public omitted env-file sentinel."""
    secret = "compose-sentinel-secret"
    compose_env_file = tmp_path / LOCAL_SERVICE_COMPOSE_ENV_FILE
    compose_env_file.parent.mkdir(parents=True, exist_ok=True)
    compose_env_file.write_text(f"CUSTOM_API_KEY={secret}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    resolved_compose_env_file = _resolve_service_log_compose_env_file(COMPOSE_ENV_FILE_OMITTED)

    values = _service_log_secret_values({}, resolved_compose_env_file)

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

    assert calls[0][2:4] == ["--env-file", str(compose_env_file)]
    assert secret not in result.stdout
    assert "<redacted>" in result.stdout


@pytest.mark.unit
def test_service_logs_default_resolves_adjacent_compose_env_file_for_redaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Default helper calls must redact provider secrets from the discovered compose env."""
    secret = "default-compose-only-provider-secret"
    compose_file = tmp_path / LOCAL_SERVICE_COMPOSE_FILE
    compose_env_file = tmp_path / LOCAL_SERVICE_COMPOSE_ENV_FILE
    nested_dir = tmp_path / "nested" / "project"
    compose_file.parent.mkdir(parents=True, exist_ok=True)
    nested_dir.mkdir(parents=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    compose_env_file.write_text(f"ANTHROPIC_AUTH_TOKEN={secret}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def success_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Record subprocess arguments and emit the compose-only secret bare."""
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout=f"{secret}\n", stderr="")

    monkeypatch.chdir(nested_dir)

    result = run_service_logs(services=[ServiceLogName.api], run_subprocess=success_run)

    assert calls[0][2:4] == ["--env-file", str(compose_env_file)]
    assert calls[0][4:6] == ["-f", str(compose_file)]
    assert secret not in result.stdout
    assert "<redacted>" in result.stdout

    run_service_logs(
        services=[ServiceLogName.api],
        compose_env_file=None,
        run_subprocess=success_run,
    )

    assert calls[1][2:4] == ["-f", str(compose_file)]


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
def test_service_logs_follow_redacts_multiline_private_key_assignment_without_exact_secret(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Keep PEM assignment context for followed streams without exact secrets."""
    pem_kind = "OPENSSH PRIVATE KEY"
    pem_body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ=="
    private_key = f"-----BEGIN {pem_kind}-----\n{pem_body}\n-----END {pem_kind}-----"

    class _FollowProcess:
        """Follow process double that streams an uncollected PEM assignment."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """Expose stdout containing a private-key assignment split by lines."""
            self.stdout = io.StringIO(f"SSH_PRIVATE_KEY={private_key}\nstatus=ready\n")
            self.stderr = io.StringIO("")

        def wait(self, timeout: float | None = None) -> int:
            """Finish immediately after the streaming threads read both pipes."""
            assert timeout is None
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FollowProcess)

    result = run_service_logs(
        services=[ServiceLogName.api],
        follow=True,
    )

    rendered = capfd.readouterr().out
    assert result == ServiceLogsResult(stdout="", stderr="")
    assert pem_kind not in rendered
    assert pem_body not in rendered
    assert "status=ready" in rendered
    assert "SSH_PRIVATE_KEY=<redacted>" in rendered


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
