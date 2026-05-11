"""Service configuration tests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from awf.common.config import DEFAULT_MIN_FREE_DISK_BYTES, Settings
from awf.service.config import (
    DEFAULT_LOCAL_SERVICE_WORK_DIR,
    _redact_database_url,
    _resolve_service_work_dir,
    local_service_environ,
    resolve_service_settings,
    service_config_payload,
)


@pytest.mark.unit
def test_agent_watchdog_defaults_are_conservative_and_documented_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)
    rendered = json.dumps(payload)

    assert settings.agent_wall_timeout_seconds == 7200
    assert settings.agent_idle_timeout_seconds == 3600
    assert payload["agent_wall_timeout_seconds"] == 7200
    assert payload["agent_idle_timeout_seconds"] == 3600
    assert "agent_wall_timeout_seconds" in rendered
    assert "agent_idle_timeout_seconds" in rendered


@pytest.mark.unit
def test_agent_watchdog_settings_flow_from_settings_to_service_settings() -> None:
    base = Settings(
        _env_file=None,
        agent_wall_timeout_seconds=1234,
        agent_idle_timeout_seconds=56,
    )

    settings = resolve_service_settings(base, environ={})

    assert settings.agent_wall_timeout_seconds == 1234
    assert settings.agent_idle_timeout_seconds == 56


@pytest.mark.unit
def test_planning_max_iterations_default_is_three_and_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert Settings(_env_file=None).planning_max_iterations_default == 3
    assert settings.planning_max_iterations_default == 3
    assert payload["planning_max_iterations_default"] == 3


@pytest.mark.unit
def test_planning_max_iterations_default_flows_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_PLANNING_MAX_ITERATIONS_DEFAULT", "4")

    settings = resolve_service_settings(Settings(_env_file=None), environ=os.environ)

    assert settings.planning_max_iterations_default == 4


@pytest.mark.unit
def test_empty_local_capacity_environment_values_are_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_LOCAL_CAPACITY_CPU_CORES", "")
    monkeypatch.setenv("AWF_LOCAL_CAPACITY_MEMORY_GB", "")
    monkeypatch.setenv("AWF_LOCAL_CAPACITY_DIND_SLOTS", "")

    settings = Settings(_env_file=None)

    assert settings.local_capacity_cpu_cores is None
    assert settings.local_capacity_memory_gb is None
    assert settings.local_capacity_dind_slots is None


@pytest.mark.unit
def test_min_free_disk_threshold_defaults_to_conservative_10_gib_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert DEFAULT_MIN_FREE_DISK_BYTES == 10 * 1024 * 1024 * 1024
    assert settings.min_free_disk_bytes == DEFAULT_MIN_FREE_DISK_BYTES
    assert payload["min_free_disk_bytes"] == DEFAULT_MIN_FREE_DISK_BYTES


@pytest.mark.unit
def test_min_free_disk_threshold_flows_from_settings_to_service_settings() -> None:
    base = Settings(_env_file=None, min_free_disk_bytes=123456)

    settings = resolve_service_settings(base, environ={"AWF_MIN_FREE_DISK_BYTES": "123456"})

    assert settings.min_free_disk_bytes == 123456


@pytest.mark.unit
def test_local_service_work_dir_defaults_to_compose_host_state_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_work_dir = tmp_path / "awf-service"
    monkeypatch.delenv("AWF_WORK_DIR", raising=False)
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"HOME": str(tmp_path), "AWF_HOST_WORK_DIR": str(host_work_dir)},
    )

    assert DEFAULT_LOCAL_SERVICE_WORK_DIR == "~/.awf/service"
    assert settings.work_dir == str(host_work_dir)


@pytest.mark.unit
def test_local_service_ignores_project_default_awf_work_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", ".awf")
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)

    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"HOME": str(tmp_path), "AWF_WORK_DIR": ".awf"},
    )

    assert settings.work_dir == str(tmp_path / ".awf" / "service")


@pytest.mark.unit
def test_compose_host_work_dir_takes_precedence_over_shell_awf_work_dir(
    tmp_path: Path,
) -> None:
    shell_work_dir = tmp_path / "project"
    host_work_dir = tmp_path / "compose-default"

    settings = resolve_service_settings(
        Settings(_env_file=None, work_dir=str(shell_work_dir)),
        environ={
            "AWF_WORK_DIR": str(shell_work_dir),
            "AWF_HOST_WORK_DIR": str(host_work_dir),
        },
    )

    assert settings.work_dir == str(host_work_dir)


@pytest.mark.unit
def test_local_service_work_dir_resolves_from_compose_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_work_dir = tmp_path / "compose-service-state"
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text(f"AWF_HOST_WORK_DIR={host_work_dir}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AWF_WORK_DIR", raising=False)
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.work_dir == str(host_work_dir)


@pytest.mark.unit
def test_project_default_awf_work_dir_does_not_hide_compose_host_work_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_work_dir = tmp_path / "compose-service-state"
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text(f"AWF_HOST_WORK_DIR={host_work_dir}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWF_WORK_DIR", ".awf")
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.work_dir == str(host_work_dir)


@pytest.mark.unit
def test_workspace_cleanup_policy_defaults_are_documented_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert settings.completed_workspace_retention_hours == 168
    assert settings.workspace_cleanup_enabled is True
    assert settings.workspace_cleanup_scan_interval_seconds == 3600
    assert settings.workspace_cleanup_batch_limit == 50
    assert payload["completed_workspace_retention_hours"] == 168
    assert payload["workspace_cleanup_enabled"] is True
    assert payload["workspace_cleanup_scan_interval_seconds"] == 3600
    assert payload["workspace_cleanup_batch_limit"] == 50


@pytest.mark.unit
def test_workspace_cleanup_policy_flows_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_COMPLETED_WORKSPACE_RETENTION_HOURS", "12")
    monkeypatch.setenv("AWF_WORKSPACE_CLEANUP_ENABLED", "false")
    monkeypatch.setenv("AWF_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("AWF_WORKSPACE_CLEANUP_BATCH_LIMIT", "7")

    settings = resolve_service_settings(Settings(_env_file=None), environ=os.environ)

    assert settings.completed_workspace_retention_hours == 12
    assert settings.workspace_cleanup_enabled is False
    assert settings.workspace_cleanup_scan_interval_seconds == 300
    assert settings.workspace_cleanup_batch_limit == 7


@pytest.mark.unit
def test_network_posture_legacy_cutoff_is_unset_by_default_and_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert settings.network_posture_open_legacy_cutoff is None
    assert payload["network_posture_open_legacy_cutoff"] is None


@pytest.mark.unit
def test_network_posture_legacy_cutoff_treats_blank_string_as_unset() -> None:
    settings = Settings(_env_file=None, network_posture_open_legacy_cutoff=" ")

    assert settings.network_posture_open_legacy_cutoff is None


@pytest.mark.unit
def test_network_posture_legacy_cutoff_flows_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_NETWORK_POSTURE_OPEN_LEGACY_CUTOFF", "2026-05-02T13:00:00Z")

    settings = resolve_service_settings(Settings(_env_file=None), environ=os.environ)
    payload = service_config_payload(settings)

    assert settings.network_posture_open_legacy_cutoff == datetime(2026, 5, 2, 13, 0, tzinfo=UTC)
    assert payload["network_posture_open_legacy_cutoff"] == "2026-05-02T13:00:00+00:00"
    json.dumps(payload)


@pytest.mark.unit
def test_local_service_config_resolves_stable_worker_node_id_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert settings.node_id == "local"
    assert payload["node_id"] == "local"


@pytest.mark.unit
def test_explicit_worker_node_id_is_preserved_for_non_local_multi_node_deployments() -> None:
    base = Settings(_env_file=None, worker_node_id="prod-node-a")

    settings = resolve_service_settings(
        base,
        environ={"AWF_WORKER_NODE_ID": "prod-node-a"},
    )

    assert settings.node_id == "prod-node-a"


@pytest.mark.unit
def test_local_service_accepts_standard_gh_token_fallback() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"GH_TOKEN": "ghp_service_token"},
    )

    assert settings.github_token == "ghp_service_token"


@pytest.mark.unit
def test_awf_github_token_precedes_standard_gh_token_fallback() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None, github_token="ghp_awf_token"),
        environ={
            "AWF_GITHUB_TOKEN": "ghp_awf_token",
            "GH_TOKEN": "ghp_standard_token",
        },
    )

    assert settings.github_token == "ghp_awf_token"


@pytest.mark.unit
def test_awf_github_token_resolves_from_explicit_service_environment() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"AWF_GITHUB_TOKEN": "ghp_explicit_awf_token"},
    )

    assert settings.github_token == "ghp_explicit_awf_token"


@pytest.mark.unit
def test_local_service_environ_loads_compose_env_with_host_override(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_GITHUB_TOKEN=ghp_compose_token\nGH_TOKEN=ghp_compose_gh_token\nEMPTY_VALUE=\n",
        encoding="utf-8",
    )

    environ = local_service_environ(
        {"AWF_GITHUB_TOKEN": "ghp_host_token", "PATH": "/usr/bin"},
        env_file=env_file,
    )

    assert environ["AWF_GITHUB_TOKEN"] == "ghp_host_token"
    assert environ["GH_TOKEN"] == "ghp_compose_gh_token"
    assert environ["EMPTY_VALUE"] == ""
    assert environ["PATH"] == "/usr/bin"


@pytest.mark.unit
def test_host_awf_host_work_dir_overrides_host_awf_work_dir(tmp_path: Path) -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={
            "AWF_WORK_DIR": str(tmp_path / "explicit-service-state"),
            "AWF_HOST_WORK_DIR": str(tmp_path / "host-service-state"),
        },
    )

    assert settings.work_dir == str(tmp_path / "host-service-state")


@pytest.mark.unit
def test_explicit_awf_work_dir_environment_is_service_work_dir(tmp_path: Path) -> None:
    work_dir = tmp_path / "explicit-service-state"

    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"AWF_WORK_DIR": str(work_dir)},
    )

    assert settings.work_dir == str(work_dir)


@pytest.mark.unit
def test_compose_file_awf_work_dir_is_used_when_host_env_has_no_work_dir(tmp_path: Path) -> None:
    work_dir = tmp_path / "compose-explicit-service-state"

    resolved = _resolve_service_work_dir(
        Settings(_env_file=None),
        environ={"AWF_WORK_DIR": str(work_dir)},
        host_environ={},
    )

    assert resolved == str(work_dir)


@pytest.mark.unit
def test_host_awf_work_dir_precedes_compose_file_default(tmp_path: Path) -> None:
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        f"AWF_HOST_WORK_DIR={tmp_path / 'compose-service-state'}\n",
        encoding="utf-8",
    )
    environ = local_service_environ(
        {"AWF_WORK_DIR": str(tmp_path / "explicit-service-state")},
        env_file=compose_env_file,
    )

    work_dir = _resolve_service_work_dir(
        Settings(_env_file=None),
        environ,
        host_environ={"AWF_WORK_DIR": str(tmp_path / "explicit-service-state")},
    )

    assert work_dir == str(tmp_path / "explicit-service-state")


@pytest.mark.unit
def test_project_default_awf_work_dir_defers_to_compose_file_host_work_dir(tmp_path: Path) -> None:
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        f"AWF_HOST_WORK_DIR={tmp_path / 'compose-service-state'}\n",
        encoding="utf-8",
    )
    environ = local_service_environ(
        {"HOME": str(tmp_path / "home"), "AWF_WORK_DIR": ".awf"},
        env_file=compose_env_file,
    )

    work_dir = _resolve_service_work_dir(
        Settings(_env_file=None),
        environ,
        host_environ={"HOME": str(tmp_path / "home"), "AWF_WORK_DIR": ".awf"},
    )

    assert work_dir == str(tmp_path / "compose-service-state")


@pytest.mark.unit
def test_redact_database_url_handles_malformed_secret_values() -> None:
    assert _redact_database_url("postgresql://user:secret@host:bad/db") == "<redacted>"
    assert _redact_database_url("") == ""


@pytest.mark.unit
def test_local_service_compose_sets_stable_worker_node_id_for_control_plane_services() -> None:
    compose_path = Path(__file__).resolve().parents[3] / "docker" / "compose" / "local-service.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    for service_name in ("api", "worker", "migrate"):
        service = compose["services"][service_name]
        assert service["environment"]["AWF_WORKER_NODE_ID"] == "local"
        assert service["environment"]["AWF_PLANNING_MAX_ITERATIONS_DEFAULT"].endswith(":-3}")
        assert service["environment"]["AWF_GITHUB_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
        assert service["environment"]["GH_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
        assert service["environment"]["GITHUB_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
