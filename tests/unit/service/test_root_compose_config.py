"""Static checks for the root Docker Compose cold-start contract."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _compose_available() -> bool:
    return (
        subprocess.run(
            ["docker", "compose", "version"],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _compose_config(*args: str) -> str:
    if not _compose_available():
        pytest.skip("docker compose is not available")
    env = {
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "HOME": os.environ.get("HOME", str(Path.home())),
        "PATH": os.environ.get("PATH", ""),
    }
    with tempfile.NamedTemporaryFile() as env_file:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                env_file.name,
                *args,
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.unit
def test_root_compose_clean_checkout_config_succeeds() -> None:
    """The root entrypoint must render without root .env or exported AWF secrets."""
    assert _compose_config("config", "--quiet") == ""


@pytest.mark.unit
def test_root_compose_reuses_local_service_project_name() -> None:
    """Root Compose and guided start must share the same persisted service volume."""
    config = yaml.safe_load(_compose_config("config"))

    assert config["name"] == "awf-local-service"


@pytest.mark.unit
def test_root_compose_clean_checkout_includes_full_local_stack() -> None:
    services = set(_compose_config("config", "--services").splitlines())

    assert services == {
        "agent-runtime",
        "api",
        "console",
        "migrate",
        "postgres",
        "worker",
    }


@pytest.mark.unit
def test_root_compose_clean_checkout_builds_expected_images() -> None:
    images = set(_compose_config("config", "--images").splitlines())

    assert {
        "awf-agent-runtime:latest",
        "awf-console:local",
        "awf-control-plane:local",
        "postgres:16-alpine",
    } <= images


@pytest.mark.unit
def test_root_compose_clean_checkout_uses_loopback_defaults() -> None:
    config = yaml.safe_load(_compose_config("config"))
    assert isinstance(config, dict)
    services = config["services"]

    assert services["api"]["environment"]["AWF_API_TOKEN"] == "local-dev-token"
    assert (
        services["api"]["environment"]["AWF_DATABASE_URL"]
        == "postgresql+asyncpg://awf:awf_dev@postgres:5432/awf"
    )
    assert services["console"]["environment"]["AWF_API_BASE_URL"] == "http://api:8000"
    assert services["console"]["environment"]["AWF_API_TOKEN"] == "local-dev-token"
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"] == "awf_dev"

    assert _published_port(services["api"], target=8000) == ("127.0.0.1", "8000")
    assert _published_port(services["console"], target=3000) == ("127.0.0.1", "3000")
    assert _published_port(services["postgres"], target=5432) == ("127.0.0.1", "5433")


@pytest.mark.unit
def test_root_compose_api_and_worker_wait_for_agent_runtime_build() -> None:
    config = yaml.safe_load(_compose_config("config"))
    services = config["services"]

    for service_name in ("api", "worker"):
        depends_on = services[service_name]["depends_on"]
        assert depends_on["agent-runtime"]["condition"] == "service_completed_successfully"


def _published_port(service: dict[str, Any], *, target: int) -> tuple[str, str]:
    for port in service.get("ports", ()):
        if port.get("target") == target:
            return str(port.get("host_ip")), str(port.get("published"))
    raise AssertionError(f"Missing published port for target {target}")
