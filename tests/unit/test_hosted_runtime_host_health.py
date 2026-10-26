"""Busy execution is healthy, but can never be reported as quiescent."""

from unittest.mock import AsyncMock, Mock

from awf.hosted_node.host_linux import LinuxSnapshot
from awf.hosted_node.host_runtime import HostRuntime
from tests.unit.test_hosted_runtime_authority import authority as authority


async def test_enabled_host_observation_preserves_health_while_work_is_active(
    authority, monkeypatch
):
    authority, _, factory = authority
    receiver = authority.receiver
    docker = Mock(quiescent=AsyncMock(return_value=False), healthy=AsyncMock(return_value=True))
    host = HostRuntime(receiver.config, factory, receiver.identity, docker)
    active = LinuxSnapshot("12345678-1234-1234-1234-123456789abc", 1, 2, 3, False, False)
    host.linux.sample = Mock(return_value=active)
    monkeypatch.setattr("awf.hosted_node.host_runtime.validate_database", AsyncMock())
    observation = await host.observe(receiver.identity)
    assert observation is not None
    assert observation.retained_state_verified
    assert not observation.quiescent
    assert host.linux.sample.call_args.kwargs == {"require_quiescent": False}
    # Even enabled execution never turns busy state into boot/recovery proof.
    assert await host.observe_boot(receiver.identity) is None


async def test_closed_host_still_requires_quiescence(authority, monkeypatch):
    authority, _, factory = authority
    receiver = authority.receiver
    config = receiver.config.model_copy(update={"runtime_enabled": False})
    docker = Mock(quiescent=AsyncMock(return_value=False), healthy=AsyncMock(return_value=True))
    host = HostRuntime(config, factory, receiver.identity, docker)
    host.linux.sample = Mock(return_value=LinuxSnapshot("boot", 1, 2, 3, False))
    assert await host.observe(receiver.identity) is None
    docker.healthy.assert_not_awaited()
