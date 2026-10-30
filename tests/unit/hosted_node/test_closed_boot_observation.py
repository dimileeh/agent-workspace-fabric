from unittest.mock import AsyncMock, Mock

import pytest

from awf.hosted_node import host_linux
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.host_linux import LinuxSnapshot
from awf.hosted_node.host_runtime import HostRuntime
from tests.unit.hosted_node.test_host_config import host_config
from tests.unit.hosted_node.test_host_linux import BOOT, IDENTITY
from tests.unit.hosted_node.test_host_linux import synthetic_linux as synthetic_linux


@pytest.mark.parametrize("runtime_enabled", [False, True])
async def test_boot_observation_does_not_require_terminal_history_or_allow_busy_execution(
    runtime_enabled,
):
    config = HostedSettings(
        owner=IDENTITY.owner,
        incarnation=IDENTITY.incarnation,
        recovery_generation=IDENTITY.recovery_generation,
        controller_token="synthetic-controller",
        material_root="/unused",
        lifecycle={"disk_ref": "disk", "database_ref": "db"},
        host=host_config(),
        runtime_enabled=runtime_enabled,
    )
    runtime = HostRuntime(config, Mock(), IDENTITY, Mock())
    runtime._observe = AsyncMock(return_value=LinuxSnapshot(BOOT, 1, 2, 3, False))
    result = await runtime.observe_boot(IDENTITY)
    assert result is not None and result.identity == IDENTITY and result.kernel_boot_id == BOOT
    runtime._observe.assert_awaited_once_with(IDENTITY, require_database_quiescent=False)


async def test_boot_observation_rejects_execution_that_is_not_quiescent():
    config = HostedSettings(
        owner=IDENTITY.owner,
        incarnation=IDENTITY.incarnation,
        recovery_generation=IDENTITY.recovery_generation,
        controller_token="synthetic-controller",
        material_root="/unused",
        lifecycle={"disk_ref": "disk", "database_ref": "db"},
        host=host_config(),
    )
    runtime = HostRuntime(config, Mock(), IDENTITY, Mock())
    runtime._observe = AsyncMock(return_value=LinuxSnapshot(BOOT, 1, 2, 3, False, False))
    assert await runtime.observe_boot(IDENTITY) is None


@pytest.mark.parametrize("cause", ["cgroup", "device", "identity", "marker"])
async def test_actual_linux_validation_still_precedes_boot_evidence(
    synthetic_linux, monkeypatch, cause
):
    probe, files, _, _ = synthetic_linux
    config = HostedSettings(
        owner=IDENTITY.owner,
        incarnation=IDENTITY.incarnation,
        recovery_generation=IDENTITY.recovery_generation,
        controller_token="synthetic-controller",
        material_root="/unused",
        lifecycle={"disk_ref": "disk", "database_ref": "db"},
        host=probe.config,
    )
    docker = Mock(quiescent=AsyncMock())
    runtime = HostRuntime(config, Mock(), IDENTITY, docker)
    runtime.linux = probe
    if cause == "cgroup":
        monkeypatch.setattr(
            host_linux,
            "small_read",
            lambda path: BOOT if "boot_id" in str(path) else "populated 1\nfrozen 0\n",
        )
    elif cause == "device":
        monkeypatch.setattr(host_linux, "block_device", lambda _: 999)
    elif cause == "identity":
        files[probe.config.identity_file]["incarnation"] = "999"
    else:
        files[probe.config.state_root / ".awf-node.json"]["database_ref"] = "foreign"
    assert await runtime.observe_boot(IDENTITY) is None
    docker.quiescent.assert_not_awaited()
