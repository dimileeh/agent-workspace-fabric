from pathlib import Path

import pytest
from pydantic import ValidationError

from awf.hosted_node.config import HostedSettings
from awf.hosted_node.host_config import HostSettings


def host_config(**changes):
    value = {
        "state_root": "/var/lib/awf",
        "device_path": "/dev/disk/by-id/state-device",
        "filesystem_uuid": "2e9aa149-9f1d-4bc8-b084-45bd3bd538b1",
        "workspaces_root": "/var/lib/awf/workspaces",
        "docker_root": "/var/lib/awf/docker",
        "execution_cgroup": "/sys/fs/cgroup/awf-execution",
        "identity_file": "/run/awf-host/identity.json",
        "bootstrap_approval": "/etc/awf-host/first-node.json",
        "docker_socket": "/run/docker.sock",
        "database_name": "node_db",
        "database_role": "node_app",
    }
    value.update(changes)
    return HostSettings(**value)


@pytest.mark.parametrize(
    "change",
    [
        {"state_root": "/"},
        {"state_root": "relative"},
        {"device_path": "/dev/../etc/file"},
        {"filesystem_uuid": "../escape"},
        {"filesystem_uuid": "2E9AA149-9F1D-4BC8-B084-45BD3BD538B1"},
        {"workspaces_root": "/elsewhere"},
        {"execution_cgroup": "/sys/fs/cgroup"},
        {"docker_root": "/elsewhere"},
        {"database_name": ""},
        {"database_role": ""},
        {"docker_socket": "tcp://host:2375"},
        {"allowed_containers": [{"id": "short", "image": "latest"}]},
    ],
)
def test_invalid_host_config_is_rejected(change):
    with pytest.raises(ValidationError):
        host_config(**change)


def test_host_composition_is_explicit_and_requires_lifecycle():
    assert host_config().state_root == Path("/var/lib/awf")
    value = {
        "owner": "org/node",
        "incarnation": "8063738394745155541",
        "recovery_generation": 1,
        "controller_token": "synthetic-controller",
        "material_root": "/run/materials",
    }
    assert HostedSettings(**value).host is None
    with pytest.raises(ValidationError):
        HostedSettings(**value, host=host_config())
