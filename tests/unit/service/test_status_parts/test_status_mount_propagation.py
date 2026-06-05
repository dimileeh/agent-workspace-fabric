"""Mount-propagation posture surfacing in collect_service_status (#400)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.service import status as status_mod
from awf.service.config import ServiceSettings


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )


@pytest.mark.unit
def test_mount_propagation_check_from_environ() -> None:
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={
            "AWF_WORK_DIR_BIND_PROPAGATION": "rshared",
            "AWF_CLAUDE_AUTH_FORCE_COPY": "false",
        },
        compose_env_file=None,
    )
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["reason"] == "MOUNT_PROPAGATION_AVAILABLE"
    assert payload["propagation"] == "rshared"
    assert payload["force_copy"] is False


@pytest.mark.unit
def test_mount_propagation_check_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_WORK_DIR_BIND_PROPAGATION=rprivate\nAWF_CLAUDE_AUTH_FORCE_COPY=true\n",
        encoding="utf-8",
    )
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={},
        compose_env_file=env_file,
    )
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["propagation"] == "rprivate"
    assert payload["force_copy"] is True


@pytest.mark.unit
def test_mount_propagation_check_unknown_when_missing() -> None:
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={},
        compose_env_file=None,
    )
    assert payload["ok"] is True
    assert payload["status"] == "unknown"
    assert payload["reason"] == "MOUNT_PROPAGATION_UNKNOWN"
    assert payload["propagation"] is None
    assert payload["force_copy"] is None


@pytest.mark.unit
def test_mount_propagation_check_environ_takes_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_WORK_DIR_BIND_PROPAGATION=rprivate\nAWF_CLAUDE_AUTH_FORCE_COPY=true\n",
        encoding="utf-8",
    )
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={
            "AWF_WORK_DIR_BIND_PROPAGATION": "rshared",
            "AWF_CLAUDE_AUTH_FORCE_COPY": "false",
        },
        compose_env_file=env_file,
    )
    assert payload["propagation"] == "rshared"
    assert payload["force_copy"] is False
