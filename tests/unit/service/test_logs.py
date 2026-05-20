"""Local service log helper tests."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from awf.service.logs import (
    DEFAULT_LOG_TAIL,
    LOCAL_SERVICE_COMPOSE_FILE,
    ServiceLogName,
    ServiceLogsError,
    _resolve_local_service_compose_file,
    _run_subprocess,
    run_service_logs,
    service_logs_command,
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
