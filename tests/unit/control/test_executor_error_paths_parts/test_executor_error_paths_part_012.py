"""Companion env-secret resume edge coverage for executor helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml
from yaml.constructor import ConstructorError

from awf.control.executor import monitor_handoff as executor_monitor_handoff
from awf.node import companion_services


def _backend_optional_env_secret_specs() -> tuple[companion_services.WorkspaceCompanionSpec, ...]:
    return executor_monitor_handoff.companion_specs_from_task_policy(
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


@pytest.mark.unit
def test_compose_string_key_loader_rejects_non_mapping_node() -> None:
    """The compose string-key loader rejects non-mapping YAML nodes."""
    loader = executor_monitor_handoff._ComposeStringKeySafeLoader("")
    node = yaml.nodes.ScalarNode("tag:yaml.org,2002:str", "not-a-mapping")

    with pytest.raises(ConstructorError, match="expected a mapping node"):
        executor_monitor_handoff._construct_compose_string_key_mapping(loader, node)


@pytest.mark.unit
def test_compose_string_key_loader_rejects_unhashable_constructed_key() -> None:
    """Unhashable compose keys are rejected instead of crashing later."""
    with pytest.raises(ConstructorError, match="found unhashable key"):
        yaml.load(
            "? [backend]\n: value\n",
            Loader=executor_monitor_handoff._ComposeStringKeySafeLoader,
        )


@pytest.mark.unit
def test_required_companion_env_secret_precheck_reports_all_unavailable_sources() -> None:
    """Resume precheck reports every missing or empty required env secret source."""
    companion_specs = (
        companion_services.WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:backend.git",
            environment_secrets=(
                companion_services.CompanionEnvironmentSecretRef(
                    target="BACKEND_REQUIRED_TOKEN",
                    value_from="BACKEND_REQUIRED_SOURCE",
                    required=True,
                ),
                companion_services.CompanionEnvironmentSecretRef(
                    target="BACKEND_OPTIONAL_TOKEN",
                    value_from="BACKEND_OPTIONAL_SOURCE",
                    required=False,
                ),
            ),
        ),
        companion_services.WorkspaceCompanionSpec(
            name="worker",
            repo_url="git@example.com:worker.git",
            environment_secrets=(
                companion_services.CompanionEnvironmentSecretRef(
                    target="WORKER_REQUIRED_TOKEN",
                    value_from="WORKER_REQUIRED_SOURCE",
                    required=True,
                ),
            ),
        ),
    )

    with pytest.raises(executor_monitor_handoff.CompanionEnvSecretPrecheckError) as exc_info:
        executor_monitor_handoff._precheck_required_companion_env_secrets_for_resume(
            companion_specs=companion_specs,
            environ={"WORKER_REQUIRED_SOURCE": ""},
        )

    assert exc_info.value.reason_code == companion_services.COMPANION_ENV_SECRET_SOURCE_MISSING
    stderr = exc_info.value.stderr
    assert companion_services.COMPANION_ENV_SECRET_SOURCE_MISSING in stderr
    assert companion_services.COMPANION_ENV_SECRET_SOURCE_EMPTY in stderr
    assert "companion=backend, target=BACKEND_REQUIRED_TOKEN" in stderr
    assert "source=BACKEND_REQUIRED_SOURCE" in stderr
    assert "companion=worker, target=WORKER_REQUIRED_TOKEN" in stderr
    assert "source=WORKER_REQUIRED_SOURCE" in stderr
    assert "BACKEND_OPTIONAL_TOKEN" not in stderr


@pytest.mark.unit
def test_required_companion_env_secret_precheck_allows_present_and_unsupported_refs() -> None:
    """The resume precheck allows present env secrets and future providers."""
    companion_specs = (
        companion_services.WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:backend.git",
            environment_secrets=(
                companion_services.CompanionEnvironmentSecretRef(
                    target="PRESENT_TOKEN",
                    value_from="PRESENT_SOURCE",
                    required=True,
                ),
                companion_services.CompanionEnvironmentSecretRef(
                    target="OPTIONAL_TOKEN",
                    value_from="MISSING_OPTIONAL_SOURCE",
                    required=False,
                ),
                companion_services.CompanionEnvironmentSecretRef(
                    target="FUTURE_TOKEN",
                    value_from="MISSING_FUTURE_SOURCE",
                    provider="future",
                    kind="env",
                    required=True,
                ),
            ),
        ),
    )

    executor_monitor_handoff._precheck_required_companion_env_secrets_for_resume(
        companion_specs=companion_specs,
        environ={"PRESENT_SOURCE": "raw-secret"},
    )


@pytest.mark.unit
def test_companion_env_secret_refresh_read_failure_logs_warning(tmp_path: Path) -> None:
    """Optional env-secret refresh logs read failures without breaking resume."""
    compose_file = tmp_path / "compose.yml"
    compose_file.mkdir()
    companion_specs = _backend_optional_env_secret_specs()

    with structlog.testing.capture_logs() as captured:
        executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
            workspace_id="ws_read_failed",
            compose_file=compose_file,
            companion_specs=companion_specs,
            environ={},
        )

    assert any(
        entry["event"] == "executor.resume_companion_env_secret_refresh_read_failed"
        and entry["workspace_id"] == "ws_read_failed"
        and entry["compose_file"] == str(compose_file)
        for entry in captured
    )


@pytest.mark.unit
def test_companion_env_secret_refresh_parse_failure_logs_warning(tmp_path: Path) -> None:
    """Optional env-secret refresh logs invalid Compose YAML without mutating it."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: [", encoding="utf-8")
    companion_specs = _backend_optional_env_secret_specs()

    with structlog.testing.capture_logs() as captured:
        executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
            workspace_id="ws_parse_failed",
            compose_file=compose_file,
            companion_specs=companion_specs,
            environ={},
        )

    assert any(
        entry["event"] == "executor.resume_companion_env_secret_refresh_parse_failed"
        and entry["workspace_id"] == "ws_parse_failed"
        and entry["compose_file"] == str(compose_file)
        for entry in captured
    )


@pytest.mark.unit
def test_companion_env_secret_refresh_noops_when_compose_has_no_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh avoids writing unchanged compose payloads."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    environment:
      APP_ENV: test
""".lstrip(),
        encoding="utf-8",
    )
    companion_specs = _backend_optional_env_secret_specs()

    def _unexpected_write(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("refresh should not write unchanged compose payload")

    monkeypatch.setattr(executor_monitor_handoff, "_atomic_write_text", _unexpected_write)

    executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
        workspace_id="ws_noop_refresh",
        compose_file=compose_file,
        companion_specs=companion_specs,
        environ={},
    )

    assert "APP_ENV: test" in compose_file.read_text(encoding="utf-8")


@pytest.mark.unit
def test_companion_env_secret_refresh_write_failure_logs_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh logs write failures and leaves the original compose file intact."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    environment:
      OPTIONAL_TOKEN: "${OPTIONAL_TOKEN_SOURCE:-}"
""".lstrip(),
        encoding="utf-8",
    )
    companion_specs = _backend_optional_env_secret_specs()

    def _raise_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(executor_monitor_handoff, "_atomic_write_text", _raise_write)

    with structlog.testing.capture_logs() as captured:
        executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
            workspace_id="ws_write_failed",
            compose_file=compose_file,
            companion_specs=companion_specs,
            environ={},
        )

    assert any(
        entry["event"] == "executor.resume_companion_env_secret_refresh_write_failed"
        and entry["workspace_id"] == "ws_write_failed"
        and entry["compose_file"] == str(compose_file)
        for entry in captured
    )
    assert "OPTIONAL_TOKEN" in compose_file.read_text(encoding="utf-8")


@pytest.mark.unit
def test_companion_env_secret_refresh_avoids_direct_target_file_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh writes compose changes atomically via a temp file."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    environment:
      OPTIONAL_TOKEN: "${OPTIONAL_TOKEN_SOURCE:-}"
""".lstrip(),
        encoding="utf-8",
    )
    companion_specs = _backend_optional_env_secret_specs()
    original_write_text = Path.write_text
    direct_target_writes: list[str] = []

    def _reject_direct_target_write(
        path: Path,
        data: str,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        if path == compose_file:
            direct_target_writes.append(str(path))
            raise OSError("direct compose-file write should not be used")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _reject_direct_target_write)

    executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
        workspace_id="ws_atomic_refresh",
        compose_file=compose_file,
        companion_specs=companion_specs,
        environ={},
    )

    assert direct_target_writes == []
    assert "OPTIONAL_TOKEN" not in compose_file.read_text(encoding="utf-8")


@pytest.mark.unit
def test_atomic_write_text_removes_temporary_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic compose writes clean up temporary files after replace errors."""
    target = tmp_path / "compose.yml"
    target.write_text("old", encoding="utf-8")
    original_replace = Path.replace

    def _raise_replace(path: Path, target_path: Path | str) -> Path:
        """Raise only for the temporary file replace under test."""
        if path.parent == tmp_path and path.name.startswith(".compose.yml."):
            raise OSError("replace failed")
        return original_replace(path, target_path)

    monkeypatch.setattr(Path, "replace", _raise_replace)

    with pytest.raises(OSError, match="replace failed"):
        executor_monitor_handoff._atomic_write_text(target, "new", encoding="utf-8")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".compose.yml.*.tmp")) == []
