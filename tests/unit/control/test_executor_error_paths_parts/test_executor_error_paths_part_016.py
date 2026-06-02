"""Companion-env secret-refresh + compose-environment error-path coverage.

Split out of ``test_executor_error_paths_part_006.py`` to keep each first-party
source file under the maintainability line limit (see
``tests/unit/test_core_decomposition_maintainability.py``). These exercise the
``monitor_handoff`` companion-env secret refresh and compose-environment
manipulation helpers; the executor coverage-edge cases stay in part_006.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
import yaml

from awf.control.executor import monitor_handoff as executor_monitor_handoff
from awf.control.executor import monitor_handoff_companion_env
from awf.node import companion_services


@pytest.mark.unit
def test_companion_env_secret_refresh_preserves_required_compose_interpolation(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    environment:
      REQUIRED_TOKEN: "${REQUIRED_TOKEN_SOURCE:?COMPANION_ENV_SECRET_SOURCE_MISSING_OR_COMPANION_ENV_SECRET_SOURCE_EMPTY: companion=backend, target=REQUIRED_TOKEN, provider=env, source=REQUIRED_TOKEN_SOURCE}"
      OPTIONAL_TOKEN: "${OPTIONAL_TOKEN_SOURCE:-}"
""".lstrip(),
        encoding="utf-8",
    )
    companion_specs = executor_monitor_handoff.companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:x/backend.git",
                    "environment_secrets": {
                        "REQUIRED_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "REQUIRED_TOKEN_SOURCE",
                            "required": True,
                        },
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        },
                    },
                }
            ],
        }
    )

    executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
        workspace_id="ws_interpolation",
        compose_file=compose_file,
        companion_specs=companion_specs,
        environ={"REQUIRED_TOKEN_SOURCE": "raw-required-secret"},
    )

    rendered = compose_file.read_text(encoding="utf-8")
    assert "OPTIONAL_TOKEN" not in rendered
    assert "'${REQUIRED_TOKEN_SOURCE:?" not in rendered
    assert 'REQUIRED_TOKEN: "${REQUIRED_TOKEN_SOURCE:?' in rendered
    assert "raw-required-secret" not in rendered


@pytest.mark.unit
def test_companion_env_secret_refresh_preserves_yaml_boolean_service_name_as_string(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  on:
    image: ghcr.io/example/on:latest
    environment:
      OPTIONAL_TOKEN: "${OPTIONAL_TOKEN_SOURCE:-}"
""".lstrip(),
        encoding="utf-8",
    )
    companion_specs = executor_monitor_handoff.companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "on",
                    "repo_url": "git@github.com:x/on.git",
                    "environment_secrets": {
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        },
                    },
                }
            ],
        }
    )

    executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
        workspace_id="ws_yaml_boolean_service",
        compose_file=compose_file,
        companion_specs=companion_specs,
        environ={},
    )

    rendered = compose_file.read_text(encoding="utf-8")
    parsed = yaml.safe_load(rendered)
    services = parsed["services"]
    assert "on" in services
    assert True not in services
    assert services["on"]["image"] == "ghcr.io/example/on:latest"
    assert "environment" not in services["on"]
    assert "OPTIONAL_TOKEN" not in rendered
    assert "true:" not in rendered
    assert "raw-optional-secret" not in rendered


@pytest.mark.unit
def test_companion_env_module_logs_under_its_own_module_name() -> None:
    """Moved companion-env helpers log under their own module, not quality_gates.

    The processor chain (``structlog.stdlib.add_logger_name``) renders the
    ``get_logger`` name into the ``logger`` field, which production log filtering
    keys off. After the resume-refresh helpers moved into this sibling module,
    their logger must follow the ``get_logger(__name__)`` convention so events
    are attributed to ``monitor_handoff_companion_env`` rather than inheriting
    the ``quality_gates`` logger name.
    """
    from awf.control.executor import quality_gates

    module_logger = monitor_handoff_companion_env._log
    assert module_logger is not quality_gates._log
    assert module_logger._logger_factory_args == (
        "awf.control.executor.monitor_handoff_companion_env",
    )


@pytest.mark.unit
def test_companion_env_secret_refresh_logs_warning_when_reformatting_compose_file(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
# operator note
services:
  backend:
    environment:
      OPTIONAL_TOKEN: "${OPTIONAL_TOKEN_SOURCE:-}"
""".lstrip(),
        encoding="utf-8",
    )
    companion_specs = executor_monitor_handoff.companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:x/backend.git",
                    "environment_secrets": {
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        },
                    },
                }
            ],
        }
    )

    with structlog.testing.capture_logs() as captured:
        executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
            workspace_id="ws_reformat_warning",
            compose_file=compose_file,
            companion_specs=companion_specs,
            environ={},
        )

    assert any(
        entry["event"] == "executor.resume_companion_env_secret_refresh_reformatted"
        and entry["workspace_id"] == "ws_reformat_warning"
        and entry["compose_file"] == str(compose_file)
        and entry["removed_count"] == 1
        and entry["restored_count"] == 0
        for entry in captured
    )
    rendered = compose_file.read_text(encoding="utf-8")
    assert "OPTIONAL_TOKEN" not in rendered
    assert "operator note" not in rendered


@pytest.mark.unit
def test_present_optional_companion_env_secret_refs_uses_public_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def _fake_optional_env_secret_compose_placeholder(value_from: str) -> str:
        captured.append(value_from)
        return "${CANONICAL:-sentinel}"

    monkeypatch.setattr(
        monitor_handoff_companion_env,
        "optional_env_secret_compose_placeholder",
        _fake_optional_env_secret_compose_placeholder,
    )
    companion_specs = (
        companion_services.WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:api.git",
            environment_secrets=(
                companion_services.CompanionEnvironmentSecretRef(
                    target="OPTIONAL_TOKEN",
                    value_from="OPTIONAL_TOKEN_SOURCE",
                    required=False,
                ),
            ),
        ),
    )

    assert executor_monitor_handoff._present_optional_companion_env_secret_refs(
        companion_specs=companion_specs,
        environ={"OPTIONAL_TOKEN_SOURCE": "raw-optional-secret"},
    ) == {"backend": {"OPTIONAL_TOKEN": "${CANONICAL:-sentinel}"}}
    assert captured == ["OPTIONAL_TOKEN_SOURCE"]


@pytest.mark.unit
def test_remove_compose_environment_targets_handles_non_mapping_payloads() -> None:
    """Environment target removal treats malformed compose payloads as no-ops."""
    assert (
        executor_monitor_handoff._remove_compose_environment_targets(
            None,
            {"backend": {"TOKEN"}},
        )
        == 0
    )
    assert (
        executor_monitor_handoff._remove_compose_environment_targets(
            {"services": []},
            {"backend": {"TOKEN"}},
        )
        == 0
    )
    assert (
        executor_monitor_handoff._remove_compose_environment_targets(
            {"services": {"backend": "not-a-mapping"}},
            {"backend": {"TOKEN"}},
        )
        == 0
    )


@pytest.mark.unit
def test_remove_compose_environment_targets_retains_non_matching_list_items() -> None:
    """Removing one environment list target preserves unrelated entries."""
    payload: dict[str, object] = {
        "services": {
            "backend": {
                "environment": [
                    "TOKEN=${TOKEN_SOURCE:-}",
                    "APP_ENV=test",
                    {"OTHER": "mapping item"},
                ],
            }
        }
    }

    removed = executor_monitor_handoff._remove_compose_environment_targets(
        payload,
        {"backend": {"TOKEN"}},
    )

    assert removed == 1
    assert payload == {
        "services": {
            "backend": {
                "environment": [
                    "APP_ENV=test",
                    {"OTHER": "mapping item"},
                ],
            }
        }
    }


@pytest.mark.unit
def test_restore_compose_environment_refs_handles_non_mapping_payloads() -> None:
    """Environment ref restoration treats malformed compose payloads as no-ops."""
    assert (
        executor_monitor_handoff._restore_compose_environment_refs(
            None,
            {"backend": {"TOKEN": "${TOKEN_SOURCE:-}"}},
        )
        == 0
    )
    assert (
        executor_monitor_handoff._restore_compose_environment_refs(
            {"services": []},
            {"backend": {"TOKEN": "${TOKEN_SOURCE:-}"}},
        )
        == 0
    )
    assert (
        executor_monitor_handoff._restore_compose_environment_refs(
            {"services": {"backend": "not-a-mapping"}},
            {"backend": {"TOKEN": "${TOKEN_SOURCE:-}"}},
        )
        == 0
    )


@pytest.mark.unit
def test_restore_compose_environment_refs_skips_empty_refs_and_creates_missing_environment() -> (
    None
):
    """Restore skips empty service refs and creates missing environment maps."""
    payload: dict[str, object] = {
        "services": {
            "backend": {"image": "example/backend:latest"},
            "worker": {"environment": {"APP_ENV": "test"}},
        }
    }

    restored = executor_monitor_handoff._restore_compose_environment_refs(
        payload,
        {
            "backend": {"TOKEN": "${TOKEN_SOURCE:-}"},
            "worker": {},
        },
    )

    assert restored == 1
    assert payload == {
        "services": {
            "backend": {
                "image": "example/backend:latest",
                "environment": {"TOKEN": "${TOKEN_SOURCE:-}"},
            },
            "worker": {"environment": {"APP_ENV": "test"}},
        }
    }


@pytest.mark.unit
def test_restore_compose_environment_list_refs_skips_non_string_items_and_appends_missing() -> None:
    """List restoration preserves non-string entries and appends missing refs."""
    environment: list[object] = [
        {"APP_ENV": "test"},
        "EXISTING_TOKEN=${EXISTING_TOKEN_SOURCE:-}",
    ]

    restored = executor_monitor_handoff._restore_compose_environment_list_refs(
        environment,
        {
            "EXISTING_TOKEN": "${EXISTING_TOKEN_SOURCE:-}",
            "NEW_TOKEN": "${NEW_TOKEN_SOURCE:-}",
        },
    )

    assert restored == 1
    assert environment == [
        {"APP_ENV": "test"},
        "EXISTING_TOKEN=${EXISTING_TOKEN_SOURCE:-}",
        "NEW_TOKEN=${NEW_TOKEN_SOURCE:-}",
    ]


@pytest.mark.unit
def test_restore_compose_environment_list_refs_counts_duplicate_targets_once() -> None:
    environment: list[object] = [
        "OPTIONAL_TOKEN=stale-one",
        "OPTIONAL_TOKEN=stale-two",
        "APP_ENV=test",
    ]

    restored_count = executor_monitor_handoff._restore_compose_environment_list_refs(
        environment,
        {"OPTIONAL_TOKEN": "${OPTIONAL_TOKEN_SOURCE:-}"},
    )

    assert restored_count == 1
    assert environment == [
        "OPTIONAL_TOKEN=${OPTIONAL_TOKEN_SOURCE:-}",
        "OPTIONAL_TOKEN=${OPTIONAL_TOKEN_SOURCE:-}",
        "APP_ENV=test",
    ]


@pytest.mark.unit
def test_companion_env_secret_refresh_preserves_list_environment_format_when_restoring_after_emptying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_TOKEN_SOURCE", raising=False)
    monkeypatch.setenv("PRESENT_TOKEN_SOURCE", "raw-present-secret")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    environment:
      - MISSING_TOKEN=${MISSING_TOKEN_SOURCE:-}
""".lstrip(),
        encoding="utf-8",
    )
    companion_specs = executor_monitor_handoff.companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:x/backend.git",
                    "environment_secrets": {
                        "MISSING_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "MISSING_TOKEN_SOURCE",
                            "required": False,
                        },
                        "PRESENT_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "PRESENT_TOKEN_SOURCE",
                            "required": False,
                        },
                    },
                }
            ],
        }
    )

    executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
        workspace_id="ws_list_restore",
        compose_file=compose_file,
        companion_specs=companion_specs,
        environ={"PRESENT_TOKEN_SOURCE": "raw-present-secret"},
    )

    rendered = compose_file.read_text(encoding="utf-8")
    parsed = yaml.safe_load(rendered)
    assert parsed["services"]["backend"]["environment"] == [
        "PRESENT_TOKEN=${PRESENT_TOKEN_SOURCE:-}",
    ]
    assert "MISSING_TOKEN" not in rendered
    assert "raw-present-secret" not in rendered
