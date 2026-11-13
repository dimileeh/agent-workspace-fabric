"""Busy execution is healthy, but can never be reported as quiescent."""

from unittest.mock import AsyncMock, Mock

import httpx

from awf.hosted_node.host_docker import DockerObservation
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


async def test_closed_busy_host_is_denied_but_still_names_its_physical_blockers(authority):
    authority, _, factory = authority
    receiver = authority.receiver
    config = receiver.config.model_copy(update={"runtime_enabled": False})
    docker = Mock(
        quiescent=AsyncMock(return_value=False),
        healthy=AsyncMock(return_value=True),
        unexpected_container=AsyncMock(return_value=True),
    )
    host = HostRuntime(config, factory, receiver.identity, docker)
    busy = LinuxSnapshot("boot", 1, 2, 3, False, False)

    def sample(*, require_quiescent=True):
        if require_quiescent:
            raise ValueError("HOST_TASKS_ACTIVE")
        return busy

    host.linux.sample = Mock(side_effect=sample)
    assert await host.observe(receiver.identity) is None
    assert await host.physical_blockers(receiver.identity) == (
        "DOCKER_UNEXPECTED_CONTAINER",
        "CGROUP_POPULATED",
    )
    # Naming blockers is diagnostic only: it never pins or approves host state.
    assert host.baseline is None
    docker.healthy.assert_not_awaited()
    docker.unexpected_container.return_value = False
    assert await host.physical_blockers(receiver.identity) == ("CGROUP_POPULATED",)
    host.linux.sample = Mock(return_value=LinuxSnapshot("boot", 1, 2, 3, False))
    assert await host.physical_blockers(receiver.identity) == ()


async def test_physical_blockers_refuse_other_hosts_and_failed_samples(authority):
    authority, _, factory = authority
    receiver = authority.receiver
    docker = Mock(unexpected_container=AsyncMock(return_value=True))
    host = HostRuntime(receiver.config, factory, receiver.identity, docker)
    other = receiver.identity.model_copy(update={"boot_epoch": receiver.identity.boot_epoch + 1})
    assert await host.physical_blockers(other) == ()
    host.baseline = LinuxSnapshot("boot", 1, 2, 3, True)
    host.linux.sample = Mock(return_value=LinuxSnapshot("other-boot", 1, 2, 3, True, False))
    assert await host.physical_blockers(receiver.identity) == ()
    host.linux.sample = Mock(side_effect=ValueError("CGROUP_V2_REQUIRED"))
    assert await host.physical_blockers(receiver.identity) == ()
    docker.unexpected_container.assert_not_awaited()


async def test_unavailable_docker_is_not_named_an_unexpected_container(authority):
    authority, _, factory = authority
    receiver = authority.receiver
    config = receiver.config.model_copy(update={"runtime_enabled": False})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)), base_url="http://docker"
    ) as client:
        docker = DockerObservation(config.host, client)
        host = HostRuntime(config, factory, receiver.identity, docker)
        host.linux.sample = Mock(return_value=LinuxSnapshot("boot", 1, 2, 3, False, False))
        assert not await docker.quiescent(receiver.identity.owner)
        assert await host.physical_blockers(receiver.identity) == ("CGROUP_POPULATED",)
