"""Host failures stay closed and diagnostics never echo arbitrary exception text."""

from unittest.mock import AsyncMock, Mock

import pytest
from structlog.testing import capture_logs

from awf.hosted_node.config import HostedSettings
from awf.hosted_node.host_runtime import HostRuntime
from awf.hosted_node.schemas import Identity
from tests.unit.hosted_node.test_host_config import host_config


@pytest.mark.parametrize("operation", ["observe", "observe_boot", "recover"])
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ValueError("HOST_TASKS_ACTIVE"), "HOST_TASKS_ACTIVE"),
        (ValueError("HOST_DATABASE_UNSAFE"), "HOST_DATABASE_UNSAFE"),
        (ValueError("synthetic-private-value"), "HOST_OBSERVATION_FAILED"),
        (OSError("synthetic-private-value"), "HOST_OBSERVATION_FAILED"),
        (RuntimeError("HOST_TASKS_ACTIVE"), "HOST_OBSERVATION_FAILED"),
    ],
)
async def test_host_observation_diagnostics_are_allowlisted(operation, error, reason):
    config = HostedSettings(
        owner="org/node",
        incarnation="123",
        recovery_generation=1,
        controller_token="synthetic-controller",
        material_root="/unused",
        lifecycle={"disk_ref": "disk", "database_ref": "db"},
        host=host_config(),
    )
    identity = Identity(owner="org/node", incarnation="123", recovery_generation=1, boot_epoch=1)
    runtime = HostRuntime(config, Mock(), identity, Mock())
    runtime._observe = AsyncMock(side_effect=error)
    args = (identity, None, "first-node") if operation == "recover" else (identity,)
    with capture_logs() as logs:
        assert await getattr(runtime, operation)(*args) is None
    assert len(logs) == 1
    assert logs[0]["event"] == f"hosted_node.{operation}_denied"
    assert logs[0]["reason_code"] == reason
    assert "synthetic-private-value" not in str(logs)
