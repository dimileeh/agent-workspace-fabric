"""Local service log helper tests."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest
import yaml

from awf.service.environment import cleared_docker_cli_client_keys, docker_cli_client_environ
from awf.service.logs import (
    ServiceLogName,
    ServiceLogsError,
    _resolve_local_service_compose_file,
    run_service_logs,
    service_logs_command,
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


@pytest.mark.unit
def test_docker_cli_client_environ_reports_cleared_keys_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not encode Docker CLI scrub operations as empty subprocess env values."""
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")

    service_environ = {"DOCKER_TLS_VERIFY": ""}

    assert docker_cli_client_environ(service_environ) == {}
    assert cleared_docker_cli_client_keys(service_environ) == frozenset({"DOCKER_TLS_VERIFY"})


@pytest.mark.unit
def test_service_logs_command_defaults_and_follow_flag() -> None:
    command = service_logs_command(services=[], tail=25, compose_file=Path("compose.yml"))
    follow_command = service_logs_command(
        services=[ServiceLogName.postgres],
        tail=50,
        follow=True,
        compose_file=Path("compose.yml"),
    )
    env_file_command = service_logs_command(
        services=[ServiceLogName.worker],
        tail=10,
        compose_file=Path("docker/compose/local-service.yml"),
        compose_env_file=Path("docker/compose/.env"),
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
    assert env_file_command == [
        "docker",
        "compose",
        "--env-file",
        "docker/compose/.env",
        "-f",
        "docker/compose/local-service.yml",
        "logs",
        "--tail",
        "10",
        "worker",
    ]


@pytest.mark.unit
def test_resolve_local_service_compose_file_returns_custom_path_without_search(
    tmp_path: Path,
) -> None:
    custom_compose = tmp_path / "compose.custom.yml"

    assert _resolve_local_service_compose_file(custom_compose) == custom_compose


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_returns_captured_output_for_non_follow_success() -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[-1] == "worker"
        assert kwargs == {"check": False, "capture_output": True, "text": True, "env": None}
        return subprocess.CompletedProcess(args, returncode=0, stdout="out", stderr="err")

    result = run_service_logs(services=[ServiceLogName.worker], run_subprocess=_run)

    assert result.stdout == "out"
    assert result.stderr == "err"


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_mirrors_awf_docker_host_into_subprocess_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    service_environ = {
        "AWF_DOCKER_HOST": docker_host,
        "DOCKER_HOST": "unix:///stale-docker.sock",
        "PATH": "/service/bin",
    }
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)
    monkeypatch.setenv("PATH", "/caller/bin")

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert env["PATH"] == "/caller/bin"
    assert "AWF_DOCKER_HOST" not in env


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_preserves_resolved_docker_tls_client_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_host = f"tcp://{tmp_path / 'docker-host'}:2376"
    cert_path = str(tmp_path / "certs")
    service_environ = {
        "AWF_DOCKER_HOST": docker_host,
        "DOCKER_TLS_VERIFY": "1",
        "DOCKER_CERT_PATH": cert_path,
        "AWF_API_TOKEN": "service-token",
    }
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    for key in ("AWF_DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "AWF_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert env["DOCKER_TLS_VERIFY"] == "1"
    assert env["DOCKER_CERT_PATH"] == cert_path
    assert "AWF_DOCKER_HOST" not in env
    assert "AWF_API_TOKEN" not in env


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_removes_stale_caller_docker_host_variants_when_awf_host_is_forced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    service_environ = {"AWF_DOCKER_HOST": docker_host}
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DoCkEr_HoSt", "unix:///caller-stale-docker.sock")

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert [key for key in env if key.upper() == "DOCKER_HOST"] == ["DOCKER_HOST"]


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_clears_docker_context_when_awf_docker_host_is_forced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    service_environ = {
        "AWF_DOCKER_HOST": docker_host,
        "DOCKER_CONTEXT": "service-stale-context",
    }
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("DOCKER_CONTEXT", "caller-stale-context")
    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert "AWF_DOCKER_HOST" not in env
    assert env.get("DOCKER_CONTEXT") is None


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_clears_docker_context_when_docker_host_is_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    service_environ = {"DOCKER_HOST": docker_host}
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("DOCKER_CONTEXT", "caller-stale-context")
    monkeypatch.setenv("DOCKER_HOST", "unix:///caller-docker.sock")
    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert "AWF_DOCKER_HOST" not in env
    assert env.get("DOCKER_CONTEXT") is None


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_blank_docker_host_clears_stale_caller_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_environ = {"DOCKER_HOST": ""}
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("DOCKER_HOST", "unix:///caller-stale-docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "caller-stale-context")

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env.get("DOCKER_HOST") is None
    assert env.get("DOCKER_CONTEXT") is None


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_scrubs_explicitly_cleared_docker_context_without_docker_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("DOCKER_CONTEXT", "caller-stale-context")

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ={"DOCKER_CONTEXT": ""},
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert "DOCKER_CONTEXT" not in env


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_removes_stale_caller_docker_host_variants_when_docker_host_is_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    service_environ = {"DOCKER_HOST": docker_host}
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("DoCkEr_HoSt", "unix:///caller-stale-docker.sock")
    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert [key for key in env if key.upper() == "DOCKER_HOST"] == ["DOCKER_HOST"]


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_awf_docker_host_wins_over_compose_docker_host_interpolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      DOCKER_HOST: "${DOCKER_HOST:?set DOCKER_HOST}"
""",
    )
    docker_host = f"unix://{tmp_path / 'awf-docker.sock'}"
    service_environ = {
        "AWF_DOCKER_HOST": docker_host,
        "DOCKER_HOST": "unix:///compose-interpolation-docker.sock",
    }
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("DOCKER_HOST", "unix:///caller-docker.sock")
    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert "AWF_DOCKER_HOST" not in env


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_passes_derived_compose_postgres_password_to_subprocess_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service.config import local_service_environ

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  postgres:
    environment:
      POSTGRES_PASSWORD: "${AWF_POSTGRES_PASSWORD:?set AWF_POSTGRES_PASSWORD}"
""",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_DATABASE_URL=postgresql+asyncpg://awf:derived-secret@db:5432/awf\n",
        encoding="utf-8",
    )
    service_environ = local_service_environ({}, env_file=env_file)
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    for key in (
        "AWF_DOCKER_HOST",
        "DOCKER_HOST",
        "AWF_POSTGRES_PASSWORD",
        "AWF_DATABASE_URL",
        "AWF_API_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert service_environ["AWF_POSTGRES_PASSWORD"] == "derived-secret"
    assert env["AWF_POSTGRES_PASSWORD"] == "derived-secret"
    assert "AWF_DATABASE_URL" not in env
    assert "AWF_API_TOKEN" not in env
    assert "DOCKER_HOST" not in env
    assert "AWF_DOCKER_HOST" not in env


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_resolved_compose_password_overrides_stale_caller_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  postgres:
    environment:
      POSTGRES_PASSWORD: "${AWF_POSTGRES_PASSWORD:?set AWF_POSTGRES_PASSWORD}"
""",
    )
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    service_environ = {
        "AWF_DOCKER_HOST": docker_host,
        "AWF_POSTGRES_PASSWORD": "resolved-secret",
        "AWF_API_TOKEN": "service-token",
        "AWF_DATABASE_URL": "postgresql+asyncpg://awf:resolved-secret@db:5432/awf",
    }
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("AWF_POSTGRES_PASSWORD", "stale-secret")
    monkeypatch.delenv("AWF_API_TOKEN", raising=False)
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "unix:///stale-docker.sock")

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert env["AWF_POSTGRES_PASSWORD"] == "resolved-secret"
    assert "AWF_DOCKER_HOST" not in env
    assert "AWF_API_TOKEN" not in env
    assert "AWF_DATABASE_URL" not in env


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_removes_awf_docker_host_after_compose_interpolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      AWF_DOCKER_HOST: "${AWF_DOCKER_HOST:?set AWF_DOCKER_HOST}"
""",
    )
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    service_environ = {"AWF_DOCKER_HOST": docker_host}
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    for key in ("AWF_DOCKER_HOST", "AWF_DATABASE_URL", "AWF_TEST_DATABASE_URL", "AWF_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DOCKER_HOST", "unix:///stale-docker.sock")

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert env.get("AWF_DOCKER_HOST") is None


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_removes_mixed_case_awf_docker_host_after_compose_interpolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      AWF_DOCKER_HOST: "${AWF_DOCKER_HOST:?set AWF_DOCKER_HOST}"
""",
    )
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    service_environ = {"AwF_DoCkEr_HoSt": docker_host}
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("awf_docker_host", "unix:///stale-awf-docker.sock")
    monkeypatch.setenv("DOCKER_HOST", "unix:///stale-docker.sock")

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert not any(key.upper() == "AWF_DOCKER_HOST" for key in env)


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_does_not_copy_service_secrets_to_subprocess_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    service_environ = {
        "AWF_DOCKER_HOST": docker_host,
        "AWF_API_TOKEN": "service-token",
        "AWF_DATABASE_URL": "postgresql+asyncpg://awf:secret@db:5432/awf",
    }
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    for key in ("AWF_DOCKER_HOST", "AWF_API_TOKEN", "AWF_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWF_CALLER_ENV_MARKER", "present")

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert env["AWF_CALLER_ENV_MARKER"] == "present"
    assert "AWF_DOCKER_HOST" not in env
    assert "AWF_API_TOKEN" not in env
    assert "AWF_DATABASE_URL" not in env


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_preserves_compose_cli_vars_from_resolved_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_environ = {
        "COMPOSE_PROJECT_NAME": "awf-resolved-service",
        "COMPOSE_PROFILES": "ollama-bridge",
        "AWF_API_TOKEN": "service-token",
        "AWF_DATABASE_URL": "postgresql+asyncpg://awf:secret@db:5432/awf",
    }
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "stale-project")
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    monkeypatch.delenv("AWF_API_TOKEN", raising=False)
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["COMPOSE_PROJECT_NAME"] == "awf-resolved-service"
    assert env["COMPOSE_PROFILES"] == "ollama-bridge"
    assert "AWF_API_TOKEN" not in env
    assert "AWF_DATABASE_URL" not in env


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_ignores_blank_compose_cli_vars_from_resolved_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_environ = {
        "COMPOSE_PROJECT_NAME": "",
        "COMPOSE_PROFILES": "",
    }
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    assert calls[0]["env"] is None


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_blank_compose_cli_vars_clear_stale_caller_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_environ = {
        "COMPOSE_PROJECT_NAME": "",
        "COMPOSE_PROFILES": "",
    }
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "stale-project")
    monkeypatch.setenv("COMPOSE_PROFILES", "stale-profile")

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["COMPOSE_PROJECT_NAME"] == ""
    assert env["COMPOSE_PROFILES"] == ""


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_inherits_caller_compose_cli_vars_without_subprocess_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller Compose selectors should not force an explicit subprocess env."""
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "caller-project")
    monkeypatch.setenv("COMPOSE_PROFILES", "caller-profile")

    run_service_logs(
        services=[ServiceLogName.api],
        service_environ={},
        run_subprocess=_run,
    )

    assert calls[0]["env"] is None


@pytest.mark.unit
def test_compose_cli_environ_omits_caller_values_for_absent_service_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller Compose values are inherited without explicit env overrides."""
    from awf.service import environment as service_environment

    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "caller-project")
    monkeypatch.setenv("COMPOSE_PROFILES", "caller-profile")

    assert service_environment.compose_cli_environ({}) == {}


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_uses_env_file_instead_of_copying_interpolation_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      AWF_API_TOKEN: "${AWF_API_TOKEN:?set AWF_API_TOKEN}"
      AWF_DATABASE_URL: "postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD:?set AWF_POSTGRES_PASSWORD}@postgres:5432/awf"
""",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_API_TOKEN=file-token\nAWF_POSTGRES_PASSWORD=file-password\n",
        encoding="utf-8",
    )
    service_environ = {
        "AWF_API_TOKEN": "file-token",
        "AWF_POSTGRES_PASSWORD": "file-password",
    }
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    for key in service_environ:
        monkeypatch.delenv(key, raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        compose_env_file=env_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )

    args, kwargs = calls[0]
    assert "--env-file" in args
    assert kwargs["env"] is None


@pytest.mark.usefixtures("_default_local_service_compose_file")
@pytest.mark.unit
def test_service_logs_passes_compose_interpolation_values_to_subprocess_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  postgres:
    shm_size: ${AWF_POSTGRES_SHM_SIZE:-1g}
    environment:
      POSTGRES_USER: "${AWF_POSTGRES_USER:?set AWF_POSTGRES_USER}"
      PLAIN: $AWF_PLAIN_INTERPOLATION
      ESCAPED: "$${AWF_ESCAPED_INTERPOLATION}"
""",
    )
    service_environ = {
        "AWF_POSTGRES_USER": "compose-user",
        "AWF_POSTGRES_SHM_SIZE": "2g",
        "AWF_PLAIN_INTERPOLATION": "plain-value",
        "AWF_ESCAPED_INTERPOLATION": "escaped-value",
        "AWF_API_TOKEN": "service-token",
    }
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    for key in service_environ:
        monkeypatch.delenv(key, raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env["AWF_POSTGRES_USER"] == "compose-user"
    assert env["AWF_POSTGRES_SHM_SIZE"] == "2g"
    assert env["AWF_PLAIN_INTERPOLATION"] == "plain-value"
    assert "AWF_ESCAPED_INTERPOLATION" not in env
    assert "AWF_API_TOKEN" not in env


@pytest.mark.unit
def test_compose_interpolation_environ_preserves_case_distinct_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case-distinct Compose variables need exact subprocess env keys."""
    from awf.service import environment as service_environment

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      LOWER: "${my_var:?set my_var}"
      UPPER: "${MY_VAR:?set MY_VAR}"
""",
    )
    service_environ = {"MY_VAR": "service-value"}

    monkeypatch.delenv("MY_VAR", raising=False)
    monkeypatch.delenv("my_var", raising=False)

    env = service_environment.compose_interpolation_environ(
        service_environ,
        compose_file=compose_file,
        compose_env_file=None,
    )

    assert env == {"MY_VAR": "service-value", "my_var": "service-value"}


@pytest.mark.unit
def test_service_logs_omits_env_when_caller_matches_interpolation_value_and_env_file_is_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A matching caller env already overrides a stale Compose env-file value."""
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      TOKEN: "${AWF_API_TOKEN:?set AWF_API_TOKEN}"
""",
    )
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text("AWF_API_TOKEN=stale-token\n", encoding="utf-8")
    service_environ = {"AWF_API_TOKEN": "service-token"}
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    for key in (
        "AWF_DOCKER_HOST",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
        "DOCKER_API_VERSION",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWF_API_TOKEN", "service-token")

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        compose_env_file=compose_env_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )

    assert calls[0]["env"] is None


@pytest.mark.unit
def test_service_logs_ignores_unclosed_braced_compose_interpolation(tmp_path: Path) -> None:
    """Malformed braced expressions should not be treated as Compose inputs."""
    from awf.service import environment as service_environment

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      VALID: "${AWF_VALID_INTERPOLATION:-default}"
      PLAIN: "$AWF_PLAIN_INTERPOLATION"
      BROKEN: "${AWF_MISSING_BRACE_INTERPOLATION"
""",
    )

    assert service_environment.compose_interpolation_keys(compose_file) == (
        "AWF_PLAIN_INTERPOLATION",
        "AWF_VALID_INTERPOLATION",
    )


@pytest.mark.unit
def test_service_logs_surfaces_malformed_compose_yaml_and_reloads_after_fix(
    tmp_path: Path,
) -> None:
    """Malformed Compose YAML should fail loudly and recover after file changes."""
    from awf.service import environment as service_environment

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment: [
""",
    )

    with pytest.raises(yaml.YAMLError):
        service_environment.compose_interpolation_keys(compose_file)
    with pytest.raises(yaml.YAMLError):
        service_environment.compose_interpolation_keys(compose_file)

    compose_file.write_text(
        """
services:
  api:
    environment:
      TOKEN: "${AWF_FIXED_TOKEN:?set AWF_FIXED_TOKEN}"
""",
        encoding="utf-8",
    )

    assert service_environment.compose_interpolation_keys(compose_file) == ("AWF_FIXED_TOKEN",)


@pytest.mark.unit
def test_service_logs_wraps_malformed_compose_yaml_as_structured_failure(
    tmp_path: Path,
) -> None:
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment: [
""",
    )
    subprocess_calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    with pytest.raises(ServiceLogsError) as exc_info:
        run_service_logs(
            services=[ServiceLogName.api],
            compose_file=compose_file,
            service_environ={"AWF_API_TOKEN": "service-token"},
            run_subprocess=_run,
        )

    assert exc_info.value.returncode == 1
    assert "could not parse Compose YAML" in exc_info.value.detail
    assert isinstance(exc_info.value.__cause__, yaml.YAMLError)
    assert subprocess_calls == []


@pytest.mark.unit
def test_service_logs_ignores_plain_variables_inside_braced_defaults(tmp_path: Path) -> None:
    """Compose does not recursively interpolate dollar values inside defaults."""
    from awf.service import environment as service_environment

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      DEFAULTED: "${AWF_DEFAULTED_INTERPOLATION:-$AWF_LITERAL_FALLBACK}"
      PLAIN: "$AWF_PLAIN_INTERPOLATION"
""",
    )

    assert service_environment.compose_interpolation_keys(compose_file) == (
        "AWF_DEFAULTED_INTERPOLATION",
        "AWF_PLAIN_INTERPOLATION",
    )


@pytest.mark.unit
def test_service_logs_detects_interpolation_after_compose_dollar_escape(
    tmp_path: Path,
) -> None:
    """A doubled dollar escapes one literal dollar; the next dollar can interpolate."""
    from awf.service import environment as service_environment

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      ESCAPED: "$$AWF_ESCAPED_INTERPOLATION"
      BRACED_ESCAPED: "$${AWF_BRACED_ESCAPED_INTERPOLATION}"
      PLAIN_AFTER_ESCAPE: "$$$AWF_PLAIN_AFTER_ESCAPE"
      BRACED_AFTER_ESCAPE: "$$${AWF_BRACED_AFTER_ESCAPE}"
""",
    )

    assert service_environment.compose_interpolation_keys(compose_file) == (
        "AWF_BRACED_AFTER_ESCAPE",
        "AWF_PLAIN_AFTER_ESCAPE",
    )


@pytest.mark.unit
def test_service_logs_ignores_compose_mapping_key_interpolation(tmp_path: Path) -> None:
    """Compose interpolation inputs should be collected from YAML values only."""
    from awf.service import environment as service_environment

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    labels:
      "${AWF_LABEL_KEY_INTERPOLATION}": "static-label"
      static.label: "${AWF_LABEL_VALUE_INTERPOLATION}"
""",
    )

    assert service_environment.compose_interpolation_keys(compose_file) == (
        "AWF_LABEL_VALUE_INTERPOLATION",
    )


@pytest.mark.unit
def test_service_logs_caches_compose_interpolation_keys_until_file_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      TOKEN: "${AWF_CACHE_TOKEN:?set AWF_CACHE_TOKEN}"
""",
    )
    service_environ = {"AWF_CACHE_TOKEN": "token"}
    calls: list[dict[str, object]] = []
    yaml_parse_count = 0

    original_safe_load = yaml.safe_load

    def _safe_load(payload: str) -> object:
        nonlocal yaml_parse_count
        yaml_parse_count += 1
        return original_safe_load(payload)

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("awf.service.environment.yaml.safe_load", _safe_load)
    monkeypatch.delenv("AWF_CACHE_TOKEN", raising=False)

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )
    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )

    assert yaml_parse_count == 1
    for call in calls:
        env = call["env"]
        assert isinstance(env, dict)
        assert env["AWF_CACHE_TOKEN"] == "token"


@pytest.mark.unit
def test_service_logs_cache_key_does_not_retain_compose_contents(tmp_path: Path) -> None:
    from awf.service import environment as service_environment

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      TOKEN: "${AWF_CACHE_TOKEN:?set AWF_CACHE_TOKEN}"
""",
    )
    service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001

    try:
        assert service_environment.compose_interpolation_keys(compose_file) == ("AWF_CACHE_TOKEN",)

        cache_keys = list(service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE)  # noqa: SLF001
        assert len(cache_keys) == 1
        assert all("AWF_CACHE_TOKEN" not in str(cache_key) for cache_key in cache_keys)
        assert all("services:" not in str(cache_key) for cache_key in cache_keys)
    finally:
        service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001


@pytest.mark.unit
def test_service_logs_compose_interpolation_cache_uses_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service import environment as service_environment

    class RecordingLock:
        def __init__(self) -> None:
            self.enter_count = 0
            self.exit_count = 0

        def __enter__(self) -> None:
            self.enter_count += 1

        def __exit__(self, *_exc: object) -> None:
            self.exit_count += 1

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      TOKEN: "${AWF_CACHE_TOKEN:?set AWF_CACHE_TOKEN}"
""",
    )
    lock = RecordingLock()
    monkeypatch.setattr(service_environment, "_COMPOSE_INTERPOLATION_KEYS_CACHE_LOCK", lock)
    service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001

    try:
        assert service_environment.compose_interpolation_keys(compose_file) == ("AWF_CACHE_TOKEN",)
        assert service_environment.compose_interpolation_keys(compose_file) == ("AWF_CACHE_TOKEN",)
    finally:
        service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001

    assert lock.enter_count >= 2
    assert lock.exit_count == lock.enter_count


@pytest.mark.unit
def test_service_logs_compose_interpolation_cache_allows_cached_read_during_slow_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service import environment as service_environment

    cached_compose_file = tmp_path / "cached-compose.yml"
    cached_compose_file.write_text(
        """
services:
  api:
    environment:
      TOKEN: "${AWF_CACHED_TOKEN:?set AWF_CACHED_TOKEN}"
""",
        encoding="utf-8",
    )
    miss_compose_file = tmp_path / "miss-compose.yml"
    miss_compose_file.write_text(
        """
services:
  api:
    environment:
      TOKEN: "${AWF_MISS_TOKEN:?set AWF_MISS_TOKEN}"
""",
        encoding="utf-8",
    )
    original_safe_load = service_environment.yaml.safe_load
    parse_started = threading.Event()
    release_parse = threading.Event()
    cached_read_finished = threading.Event()
    errors: list[BaseException] = []
    cached_results: list[tuple[str, ...]] = []
    miss_results: list[tuple[str, ...]] = []

    def _safe_load(payload: str) -> object:
        if "AWF_MISS_TOKEN" in payload:
            parse_started.set()
            if not release_parse.wait(timeout=2):
                raise AssertionError("slow compose parse was not released")
        return original_safe_load(payload)

    def _miss_worker() -> None:
        try:
            miss_results.append(service_environment.compose_interpolation_keys(miss_compose_file))
        except BaseException as exc:  # pragma: no cover - re-raised by the main thread
            errors.append(exc)

    def _cached_read_worker() -> None:
        try:
            cached_results.append(
                service_environment.compose_interpolation_keys(cached_compose_file)
            )
            cached_read_finished.set()
        except BaseException as exc:  # pragma: no cover - re-raised by the main thread
            errors.append(exc)

    service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001
    try:
        assert service_environment.compose_interpolation_keys(cached_compose_file) == (
            "AWF_CACHED_TOKEN",
        )
        monkeypatch.setattr(service_environment.yaml, "safe_load", _safe_load)

        miss_thread = threading.Thread(target=_miss_worker)
        cached_read_thread = threading.Thread(target=_cached_read_worker)
        try:
            miss_thread.start()
            assert parse_started.wait(timeout=2)
            cached_read_thread.start()
            assert cached_read_finished.wait(timeout=1)
        finally:
            release_parse.set()
            miss_thread.join(timeout=2)
            cached_read_thread.join(timeout=2)
    finally:
        service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001

    assert not miss_thread.is_alive()
    assert not cached_read_thread.is_alive()
    assert not errors
    assert cached_results == [("AWF_CACHED_TOKEN",)]
    assert miss_results == [("AWF_MISS_TOKEN",)]


@pytest.mark.unit
def test_service_logs_compose_interpolation_cache_serializes_concurrent_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service import environment as service_environment

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      TOKEN: "${AWF_CACHE_TOKEN:?set AWF_CACHE_TOKEN}"
""",
    )
    original_safe_load = service_environment.yaml.safe_load
    active_parses = 0
    max_active_parses = 0
    parse_lock = threading.Lock()

    def _safe_load(payload: str) -> object:
        nonlocal active_parses, max_active_parses
        with parse_lock:
            active_parses += 1
            max_active_parses = max(max_active_parses, active_parses)
        try:
            time.sleep(0.05)
            return original_safe_load(payload)
        finally:
            with parse_lock:
                active_parses -= 1

    service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001
    monkeypatch.setattr(service_environment.yaml, "safe_load", _safe_load)

    errors: list[BaseException] = []
    results: list[tuple[str, ...]] = []
    start = threading.Barrier(4)

    def _worker() -> None:
        try:
            start.wait(timeout=2)
            results.append(service_environment.compose_interpolation_keys(compose_file))
        except BaseException as exc:  # pragma: no cover - re-raised by the main thread
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
    finally:
        service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001

    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert results == [("AWF_CACHE_TOKEN",)] * 4
    assert max_active_parses == 1


@pytest.mark.unit
def test_service_logs_compose_interpolation_cache_caches_unexpected_parse_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service import environment as service_environment

    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      TOKEN: "${AWF_CACHE_TOKEN:?set AWF_CACHE_TOKEN}"
""",
    )
    parse_started = threading.Event()
    release_parse = threading.Event()
    parse_count = 0
    parse_lock = threading.Lock()

    def _safe_load(_payload: str) -> object:
        nonlocal parse_count
        with parse_lock:
            parse_count += 1
            current_parse = parse_count
        if current_parse == 1:
            parse_started.set()
            if not release_parse.wait(timeout=2):
                raise AssertionError("failing compose parse was not released")
        raise RuntimeError("synthetic compose parser failure")

    service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001
    service_environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT.clear()  # noqa: SLF001
    monkeypatch.setattr(service_environment.yaml, "safe_load", _safe_load)

    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def _worker() -> None:
        try:
            start.wait(timeout=2)
            service_environment.compose_interpolation_keys(compose_file)
        except BaseException as exc:  # pragma: no cover - asserted by the main thread
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        assert parse_started.wait(timeout=2)
        time.sleep(0.05)
    finally:
        release_parse.set()
        for thread in threads:
            thread.join(timeout=2)
        service_environment._COMPOSE_INTERPOLATION_KEYS_CACHE.clear()  # noqa: SLF001
        service_environment._COMPOSE_INTERPOLATION_KEYS_INFLIGHT.clear()  # noqa: SLF001

    assert not any(thread.is_alive() for thread in threads)
    assert len(errors) == 4
    assert all(isinstance(error, RuntimeError) for error in errors)
    assert all("synthetic compose parser failure" in str(error) for error in errors)
    assert parse_count == 1


@pytest.mark.unit
def test_service_logs_reloads_compose_interpolation_keys_when_file_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_file = _write_compose_file(
        tmp_path,
        """
services:
  api:
    environment:
      FIRST: "${AWF_FIRST_TOKEN:?set AWF_FIRST_TOKEN}"
""",
    )
    subprocess_calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(kwargs)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.delenv("AWF_FIRST_TOKEN", raising=False)
    monkeypatch.delenv("AWF_SECOND_TOKEN", raising=False)
    service_environ = {
        "AWF_FIRST_TOKEN": "first-token",
        "AWF_SECOND_TOKEN": "second-token",
    }

    run_service_logs(
        services=[ServiceLogName.api],
        compose_file=compose_file,
        service_environ=service_environ,
        run_subprocess=_run,
    )
    compose_file.write_text(
        """
services:
  api:
    environment:
      SECOND: "${AWF_SECOND_TOKEN:?set AWF_SECOND_TOKEN}"
""",
        encoding="utf-8",
    )
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
    assert "AWF_SECOND_TOKEN" not in first_env
    assert second_env["AWF_SECOND_TOKEN"] == "second-token"
    assert "AWF_FIRST_TOKEN" not in second_env
