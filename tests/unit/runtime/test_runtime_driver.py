"""Runtime-driver seam contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from awf.runtime.driver import (
    WORKSPACE_EXECUTION_V1,
    LocalRuntimeDriver,
    RuntimeDriverConfig,
    WorkspaceProvisionRequest,
    WorkspaceStartRequest,
    WorkspaceStatusRequest,
    WorkspaceStopRequest,
    WorkspaceValidateRequest,
)


@pytest.mark.unit
def test_runtime_driver_config_defaults_to_local_execution_capability() -> None:
    """Verify runtime driver config defaults to local execution capability."""
    config = RuntimeDriverConfig()

    assert config.name == "local"
    assert config.capabilities == (WORKSPACE_EXECUTION_V1,)


@pytest.mark.unit
def test_runtime_driver_config_rejects_unsupported_core_driver() -> None:
    """Verify runtime driver config rejects unsupported core driver."""
    with pytest.raises(ValueError, match="Unsupported AWF Core runtime driver"):
        RuntimeDriverConfig(name="gke")


@pytest.mark.unit
async def test_local_runtime_driver_delegates_to_local_collaborators(tmp_path: Path) -> None:
    """Verify local runtime driver delegates to local collaborators."""
    calls: list[tuple[str, dict[str, Any]]] = []
    provision_result = object()
    start_result = object()
    stop_result = object()
    validate_result = object()
    status_result = object()
    compose_file = tmp_path / "compose.yml"
    worktree = tmp_path / "repo"
    companion_worktrees = (
        ("ws_1-adopted-api", "https://github.com/o/api.git"),
        ("ws_1-fork-web", "https://github.com/o/web.git"),
    )

    class _Provisioner:
        """Test helper for Provisioner."""

        async def provision(self, workspace_id: str) -> object:
            """Provision."""
            calls.append(("provision", {"workspace_id": workspace_id}))
            return provision_result

        async def provision_claimed(
            self,
            workspace_id: str,
            execution_claim_epoch: int | None = None,
        ) -> object:
            """Provision claimed."""
            calls.append(
                (
                    "provision_claimed",
                    {
                        "workspace_id": workspace_id,
                        "execution_claim_epoch": execution_claim_epoch,
                    },
                )
            )
            return provision_result

    class _Executor:
        """Test helper for Executor."""

        async def execute(
            self,
            workspace_id: str,
            *,
            execution_owner_id: str | None = None,
            execution_lease_expires_at: object = None,
        ) -> object:
            """Execute."""
            calls.append(
                (
                    "execute",
                    {
                        "workspace_id": workspace_id,
                        "execution_owner_id": execution_owner_id,
                        "execution_lease_expires_at": execution_lease_expires_at,
                    },
                )
            )
            return start_result

    class _Cleaner:
        """Test helper for Cleaner."""

        async def cleanup(self, **kwargs: object) -> object:
            """Cleanup."""
            calls.append(("cleanup", dict(kwargs)))
            return stop_result

    class _Validation:
        """Test helper for Validation."""

        async def run(self, **kwargs: object) -> object:
            """Run."""
            calls.append(("validate", dict(kwargs)))
            return validate_result

    class _Inspector:
        """Test helper for Inspector."""

        async def inspect(self, compose_project_name: str | None) -> object:
            """Inspect."""
            calls.append(("inspect", {"compose_project_name": compose_project_name}))
            return status_result

    driver = LocalRuntimeDriver(
        provisioner=_Provisioner(),
        executor=_Executor(),
        cleaner=_Cleaner(),
        validation_runner=_Validation(),
        runtime_inspector=_Inspector(),
    )

    assert driver.capabilities == (WORKSPACE_EXECUTION_V1,)
    assert (
        await driver.provision(
            WorkspaceProvisionRequest(workspace_id="ws_1", execution_claim_epoch=7)
        )
        is provision_result
    )
    assert (
        await driver.start(
            WorkspaceStartRequest(
                workspace_id="ws_1",
                execution_owner_id="node-1",
                execution_lease_expires_at=None,
            )
        )
        is start_result
    )
    assert (
        await driver.stop(
            WorkspaceStopRequest(
                workspace_id="ws_1",
                repo_url="https://github.com/o/r.git",
                compose_project_name="awf_ws_1",
                compose_file_path=compose_file,
                worktree_host_path=worktree,
                companion_worktrees=companion_worktrees,
                remove_volumes=False,
                remove_worktree=False,
            )
        )
        is stop_result
    )
    assert (
        await driver.validate(
            WorkspaceValidateRequest(
                workspace_id="ws_1",
                compose_project="awf_ws_1",
                compose_file=compose_file,
                test_commands=("pytest tests/unit/test_example.py -q",),
                requires_database=True,
                workspace_worktree=worktree,
            )
        )
        is validate_result
    )
    assert (
        await driver.status(WorkspaceStatusRequest(compose_project_name="awf_ws_1"))
        is status_result
    )

    assert calls == [
        (
            "provision_claimed",
            {"workspace_id": "ws_1", "execution_claim_epoch": 7},
        ),
        (
            "execute",
            {
                "workspace_id": "ws_1",
                "execution_owner_id": "node-1",
                "execution_lease_expires_at": None,
            },
        ),
        (
            "cleanup",
            {
                "workspace_id": "ws_1",
                "repo_url": "https://github.com/o/r.git",
                "compose_project_name": "awf_ws_1",
                "compose_file_path": compose_file,
                "worktree_host_path": worktree,
                "companion_worktrees": companion_worktrees,
                "remove_volumes": False,
                "remove_worktree": False,
            },
        ),
        (
            "validate",
            {
                "workspace_id": "ws_1",
                "compose_project": "awf_ws_1",
                "compose_file": compose_file,
                "test_commands": ["pytest tests/unit/test_example.py -q"],
                "requires_database": True,
                "workspace_worktree": worktree,
            },
        ),
        ("inspect", {"compose_project_name": "awf_ws_1"}),
    ]


@pytest.mark.unit
async def test_local_runtime_driver_uses_legacy_provision_without_claim_epoch() -> None:
    """Verify local runtime driver uses legacy provision without claim epoch."""
    calls: list[tuple[str, str]] = []
    provision_result = object()

    class _Provisioner:
        """Test helper for Provisioner."""

        async def provision(self, workspace_id: str) -> object:
            """Provision."""
            calls.append(("provision", workspace_id))
            return provision_result

        async def provision_claimed(
            self,
            workspace_id: str,
            execution_claim_epoch: int | None = None,
        ) -> object:
            """Provision claimed."""
            calls.append(("provision_claimed", workspace_id))
            return object()

    driver = LocalRuntimeDriver(
        provisioner=_Provisioner(),
        executor=object(),
        cleaner=object(),
        validation_runner=object(),
    )

    assert (
        await driver.provision(WorkspaceProvisionRequest(workspace_id="ws_legacy"))
        is provision_result
    )
    assert calls == [("provision", "ws_legacy")]
