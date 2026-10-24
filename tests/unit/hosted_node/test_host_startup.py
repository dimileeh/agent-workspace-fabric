import os
import stat
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from awf.hosted_node import host_startup
from awf.hosted_node.host_linux import LinuxSnapshot
from tests.unit.hosted_node.test_host_config import host_config


def test_single_host_lease_is_real_nonblocking_flock(tmp_path, monkeypatch):
    path = tmp_path / ".awf-host.lock"
    path.touch(mode=0o444)
    config = host_config().model_copy(update={"state_root": tmp_path})
    info = tmp_path.stat()
    baseline = LinuxSnapshot("boot", info.st_dev, info.st_ino, 1, True)
    real = os.fstat

    def root_owned(fd):
        value = real(fd)
        return SimpleNamespace(
            st_uid=0,
            st_mode=value.st_mode & ~0o022 if stat.S_ISDIR(value.st_mode) else value.st_mode,
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_nlink=value.st_nlink,
        )

    monkeypatch.setattr(host_startup.os, "fstat", root_owned)
    with (
        host_startup.host_lease(config, baseline),
        pytest.raises(BlockingIOError),
        host_startup.host_lease(config, baseline),
    ):
        pytest.fail("second process lease admitted")
    with host_startup.host_lease(config, baseline):
        pass


async def test_hosted_worker_is_rejected_before_runtime_build(monkeypatch):
    from awf.common.config import get_settings
    from awf.service import worker

    monkeypatch.setenv(
        "AWF_HOSTED_NODE",
        '{"owner":"org/node","incarnation":"1","recovery_generation":1,"controller_token":"synthetic-controller","material_root":"/unused"}',
    )
    get_settings.cache_clear()
    build = Mock()
    monkeypatch.setattr(worker, "build_worker_runtime", build)
    try:
        with pytest.raises(ValueError, match="HOSTED_RUNTIME_INTEGRATION_REQUIRED"):
            await worker.run_worker(Mock(), once=True)
        build.assert_not_called()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "mode,uid",
    [
        (stat.S_IFSOCK | 0o660, 0),
        (stat.S_IFREG | 0o660, 0),
        (stat.S_IFSOCK | 0o666, 0),
        (stat.S_IFSOCK | 0o660, 123),
    ],
)
def test_docker_socket_must_be_root_owned_not_redirectable(monkeypatch, mode, uid):
    @contextmanager
    def directory(*args, **kwargs):
        yield 1

    monkeypatch.setattr(host_startup, "directory", directory)
    monkeypatch.setattr(
        host_startup.os, "stat", lambda *_args, **_kwargs: SimpleNamespace(st_mode=mode, st_uid=uid)
    )
    if stat.S_ISSOCK(mode) and not mode & 0o002 and uid == 0:
        host_startup.validate_socket(host_config())
    else:
        with pytest.raises(ValueError, match="TRUSTED_DOCKER_SOCKET_REQUIRED"):
            host_startup.validate_socket(host_config())


async def test_unconfigured_host_keeps_existing_receiver_lifespan(monkeypatch):
    from awf.hosted_node.config import HostedSettings

    config = HostedSettings(
        owner="org/node",
        incarnation="1",
        recovery_generation=1,
        controller_token="synthetic-controller",
        material_root="/unused",
    )
    receiver = Mock(close=AsyncMock())
    start = AsyncMock(return_value=receiver)
    monkeypatch.setattr(host_startup.Receiver, "start", start)
    async with host_startup.start_hosted(config, Mock()) as result:
        assert result is receiver
    receiver.close.assert_awaited_once()


async def test_production_docker_client_is_fixed_uds_no_proxy():
    async with host_startup.docker_client(host_config()) as client:
        assert str(client.base_url) == "http://docker"
        assert not client.follow_redirects and not client.trust_env
        assert client._transport._pool._uds == "/run/docker.sock"


@pytest.mark.parametrize(
    "cause", ["device", "inode", "mode", "owner", "links", "file_device", "type"]
)
def test_host_lease_rejects_changed_mount_or_untrusted_file(tmp_path, monkeypatch, cause):
    # Exercise the writable temporary-parent condition on Linux runners too.
    tmp_path.chmod(0o777)
    path = tmp_path / ".awf-host.lock"
    path.touch(mode=0o444)
    config = host_config().model_copy(update={"state_root": tmp_path})
    info = tmp_path.stat()
    baseline = LinuxSnapshot(
        "boot", info.st_dev + (cause == "device"), info.st_ino + (cause == "inode"), 1, True
    )
    real = os.fstat
    lease_fds = []

    def root_owned(fd):
        value = real(fd)
        file = stat.S_ISREG(value.st_mode)
        if file:
            lease_fds.append(fd)
        mode = value.st_mode & ~0o022 if stat.S_ISDIR(value.st_mode) else value.st_mode
        if file and cause == "type":
            mode = stat.S_IFIFO | 0o444
        return SimpleNamespace(
            st_uid=123 if file and cause == "owner" else 0,
            st_mode=mode | (0o002 if file and cause == "mode" else 0),
            st_dev=value.st_dev + (file and cause == "file_device"),
            st_ino=value.st_ino,
            st_nlink=2 if file and cause == "links" else value.st_nlink,
        )

    monkeypatch.setattr(host_startup.os, "fstat", root_owned)
    changed_mount = cause in ("device", "inode")
    reason = "HOST_LEASE_DEVICE_MISMATCH" if changed_mount else "HOST_LEASE_UNTRUSTED"
    with pytest.raises(ValueError, match=f"^{reason}$"), host_startup.host_lease(config, baseline):
        pytest.fail("unsafe lease")
    assert len(lease_fds) == (0 if changed_mount else 1)
    for fd in lease_fds:
        with pytest.raises(OSError):
            real(fd)
