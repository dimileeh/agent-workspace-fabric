"""Opt-in trusted Linux host composition. Never starts a worker or executor."""

import os
import stat
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.hosted_node.config import HostedSettings
from awf.hosted_node.host_config import HostSettings
from awf.hosted_node.host_database import validate_database
from awf.hosted_node.host_docker import DockerObservation
from awf.hosted_node.host_linux import LinuxSnapshot, directory
from awf.hosted_node.host_runtime import HostRuntime
from awf.hosted_node.physical_fence import PhysicalFenceVerifier, physical_fence_client
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.schemas import Identity


@contextmanager
def host_lease(config: HostSettings, baseline: LinuxSnapshot) -> Iterator[None]:
    import fcntl

    # Provisioned root-owned read-only file, not a runtime-created lock path.
    with directory(config.state_root, trusted=True) as root:
        info = os.fstat(root)
        if (info.st_dev, info.st_ino) != (baseline.device, baseline.state_inode):
            raise ValueError("HOST_LEASE_DEVICE_MISMATCH")
        fd = os.open(
            ".awf-host.lock",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=root,
        )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o222
            or info.st_nlink != 1
            or info.st_dev != baseline.device
        ):
            raise ValueError("HOST_LEASE_UNTRUSTED")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def validate_socket(config: HostSettings) -> None:
    with directory(config.docker_socket.parent, trusted=True) as parent:
        info = os.stat(config.docker_socket.name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o002:
        raise ValueError("TRUSTED_DOCKER_SOCKET_REQUIRED")


def docker_client(config: HostSettings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://docker",
        trust_env=False,
        follow_redirects=False,
        transport=httpx.AsyncHTTPTransport(uds=str(config.docker_socket)),
        timeout=2,
        limits=httpx.Limits(max_connections=1),
    )


@asynccontextmanager
async def start_hosted(
    config: HostedSettings, factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Receiver]:
    if config.host is None:
        receiver = await Receiver.start(config, factory)
        try:
            yield receiver
        finally:
            await receiver.close()
        return
    provisional = Identity(
        owner=config.owner,
        incarnation=config.incarnation,
        recovery_generation=config.recovery_generation,
        boot_epoch=1,
    )
    host_config = config.host
    async with AsyncExitStack() as clients:
        client = await clients.enter_async_context(docker_client(host_config))
        host = HostRuntime(config, factory, provisional, DockerObservation(host_config, client))
        if host_config.physical_fence_authority is not None:
            authority_client = await clients.enter_async_context(physical_fence_client())
            host.verify_physical_fence = PhysicalFenceVerifier(
                host_config.physical_fence_authority, config.owner, authority_client
            ).verify
        host.baseline = await host.host_io(host.linux.sample)
        await host.host_io(lambda: validate_socket(host_config))
        with host_lease(host_config, host.baseline):
            async with factory() as session:
                await validate_database(session, config.host)
            receiver = await Receiver.start(config, factory)
            try:
                host.identity = host.linux.identity = receiver.identity
                assert receiver.lifecycle is not None
                receiver.lifecycle.observe_host = host.observe
                receiver.lifecycle.observe_boot = host.observe_boot
                receiver.lifecycle.verify_recovery = host.recover
                yield receiver
            finally:
                await receiver.close()
