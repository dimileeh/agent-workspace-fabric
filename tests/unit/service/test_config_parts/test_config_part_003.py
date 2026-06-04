"""Service configuration tests."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from awf.common.config import (
    DEFAULT_MIN_FREE_DISK_BYTES,
    DEFAULT_ORPHAN_RECONCILE_SCAN_INTERVAL_SECONDS,
    Settings,
)
from awf.service.config import (
    DEFAULT_LOCAL_SERVICE_API_TOKEN,
    DEFAULT_LOCAL_SERVICE_WORK_DIR,
    local_service_environ,
    resolve_service_settings,
    service_config_payload,
)


def _write_awf_source_root(checkout: Path) -> Path:
    fake_module = checkout / "src" / "awf" / "service" / "config.py"
    fake_module.parent.mkdir(parents=True, exist_ok=True)
    fake_module.write_text("# source module placeholder\n", encoding="utf-8")
    (checkout / "src" / "awf" / "__init__.py").write_text("", encoding="utf-8")
    (checkout / "pyproject.toml").write_text("[project]\nname = 'awf'\n", encoding="utf-8")
    (checkout / "compose.yaml").write_text(
        "include:\n  - ./docker/compose/local-service.yml\n",
        encoding="utf-8",
    )
    compose_file = checkout / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True, exist_ok=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return fake_module


def _write_awf_source_checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    return checkout, _write_awf_source_root(checkout)


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
def test_local_compose_default_api_token_flows_into_service_settings() -> None:
    service_env = local_service_environ(environ={})

    settings = resolve_service_settings(Settings(_env_file=None), environ=service_env)

    assert service_env["AWF_API_TOKEN"] == DEFAULT_LOCAL_SERVICE_API_TOKEN
    assert settings.api_token == DEFAULT_LOCAL_SERVICE_API_TOKEN


@pytest.mark.unit
def test_explicit_settings_api_token_takes_precedence_over_local_compose_default() -> None:
    service_env = local_service_environ(environ={})
    base = Settings(_env_file=None, api_token="operator-token")

    settings = resolve_service_settings(base, environ=service_env)

    assert settings.api_token == "operator-token"


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
def test_service_startup_log_tail_lines_defaults_into_service_settings() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})

    assert settings.service_startup_log_tail_lines == 200


@pytest.mark.unit
def test_service_startup_log_tail_lines_flows_from_settings_to_service_settings() -> None:
    base = Settings(_env_file=None, worker_service_startup_log_tail_lines=75)

    settings = resolve_service_settings(base, environ={})

    assert settings.service_startup_log_tail_lines == 75


@pytest.mark.unit
@pytest.mark.parametrize("tail_lines", [0, -1, -200])
def test_service_settings_rejects_non_positive_tail_lines(tail_lines: int) -> None:
    base = resolve_service_settings(Settings(_env_file=None), environ={})

    with pytest.raises(ValueError, match="service_startup_log_tail_lines must be > 0"):
        replace(base, service_startup_log_tail_lines=tail_lines)


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
def test_local_service_work_dir_resolves_from_root_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_work_dir = tmp_path / "compose-service-state"
    checkout, _ = _write_awf_source_checkout(tmp_path)
    (checkout / ".env").write_text(f"AWF_HOST_WORK_DIR={host_work_dir}\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AWF_WORK_DIR", raising=False)
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.work_dir == str(host_work_dir)


@pytest.mark.unit
def test_project_default_awf_work_dir_does_not_hide_root_host_work_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_work_dir = tmp_path / "compose-service-state"
    checkout, _ = _write_awf_source_checkout(tmp_path)
    (checkout / ".env").write_text(f"AWF_HOST_WORK_DIR={host_work_dir}\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
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
def test_orphan_reconcile_defaults_are_off_and_sane() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})

    assert settings.auto_cleanup_orphans is False
    assert settings.orphan_reconcile_scan_interval_seconds == 3600.0
    assert (
        settings.classified_orphan_reap_scan_interval_seconds
        == DEFAULT_ORPHAN_RECONCILE_SCAN_INTERVAL_SECONDS
    )
    assert settings.orphan_reconcile_max_per_scan == 50
    assert settings.orphan_reconcile_min_age_hours == 168.0


@pytest.mark.unit
def test_orphan_reconcile_settings_flow_from_environment() -> None:
    base = Settings(
        _env_file=None,
        auto_cleanup_orphans=True,
        orphan_reconcile_scan_interval_seconds=900.0,
        classified_orphan_reap_scan_interval_seconds=450.0,
        orphan_reconcile_max_per_scan=7,
        orphan_reconcile_min_age_hours=12.0,
    )

    settings = resolve_service_settings(base, environ={})

    assert settings.auto_cleanup_orphans is True
    assert settings.orphan_reconcile_scan_interval_seconds == 900.0
    assert settings.classified_orphan_reap_scan_interval_seconds == 450.0
    assert settings.orphan_reconcile_max_per_scan == 7
    assert settings.orphan_reconcile_min_age_hours == 12.0
