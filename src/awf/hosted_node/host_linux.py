"""Linux retained-device, boot and task-containment observation; no mutation."""

import json
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from awf.hosted_node.host_config import HostSettings
from awf.hosted_node.schemas import Identity

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def linux_host() -> bool:
    return sys.platform == "linux"


@contextmanager
def directory(path: Path, *, trusted: bool = False) -> Iterator[int]:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("HOST_PATH_UNAVAILABLE")
    fd = os.open("/", _DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            if trusted:
                info = os.fstat(fd)
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError("TRUSTED_HOST_DIRECTORY_REQUIRED")
        yield fd
    finally:
        os.close(fd)


def small_read(path: Path, *, limit: int = 8192) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("HOST_OBSERVATION_TOO_LARGE")
    return data.decode("utf-8").strip()


def secure_json(path: Path) -> Any:
    # No symlink component or attacker-owned/writable/linked evidence file.
    with directory(path.parent, trusted=True) as parent:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_nlink != 1
                or info.st_mode & 0o222
                or not 0 < info.st_size <= 8192
            ):
                raise ValueError("TRUSTED_HOST_METADATA_REQUIRED")
            data = stream.read(8193)
    if len(data) > 8192:
        raise ValueError("HOST_OBSERVATION_TOO_LARGE")
    return json.loads(data)


def mount_for(fd: int) -> dict[str, Any]:
    mount_id = next(
        line.split()[1]
        for line in small_read(Path(f"/proc/self/fdinfo/{fd}")).splitlines()
        if line.startswith("mnt_id:")
    )
    lines = small_read(Path("/proc/self/mountinfo"), limit=2 * 1024 * 1024).splitlines()
    line = next(line for line in lines if line.split()[0] == mount_id)
    left, right = line.split(" - ")
    fields, tail = left.split(), right.split()
    major, minor = fields[2].split(":")

    def unescape(value: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)

    return {
        "device": os.makedev(int(major), int(minor)),
        "root": unescape(fields[3]),
        "target": unescape(fields[4]),
        "options": set(fields[5].split(",")),
        "fstype": tail[0],
    }


def block_device(path: Path) -> int:
    info = path.stat()  # /dev/disk/by-* are explicitly trusted device symlinks.
    if not stat.S_ISBLK(info.st_mode):
        raise ValueError("RETAINED_BLOCK_DEVICE_REQUIRED")
    return info.st_rdev


@dataclass(frozen=True)
class LinuxSnapshot:
    boot_id: str
    device: int
    state_inode: int
    cgroup_inode: int
    workspaces_empty: bool


class LinuxObservation:
    def __init__(self, config: HostSettings, identity: Identity, disk_ref: str, database_ref: str):
        self.config, self.identity = config, identity
        self.disk_ref, self.database_ref = disk_ref, database_ref

    def sample(self) -> LinuxSnapshot:
        if not linux_host():
            raise ValueError("LINUX_HOST_REQUIRED")
        config = self.config
        with directory(config.state_root, trusted=True) as root:
            info, mount = os.fstat(root), mount_for(root)
            if (
                info.st_uid != 0
                or info.st_mode & 0o022
                or mount["root"] != "/"
                or mount["target"] != str(config.state_root)
                or mount["fstype"] not in ("ext4", "xfs")
                or "rw" not in mount["options"]
                or info.st_dev != mount["device"]
                or block_device(config.device_path) != info.st_dev
                or block_device(Path("/dev/disk/by-uuid") / config.filesystem_uuid) != info.st_dev
            ):
                raise ValueError("RETAINED_DISK_MISMATCH")
            if secure_json(config.state_root / ".awf-node.json") != {
                "owner": self.identity.owner,
                "disk_ref": self.disk_ref,
                "database_ref": self.database_ref,
                "filesystem_uuid": config.filesystem_uuid,
            }:
                raise ValueError("RETAINED_MARKER_MISMATCH")
            with directory(config.workspaces_root) as work, directory(config.docker_root) as docker:
                if os.fstat(work).st_dev != info.st_dev or os.fstat(docker).st_dev != info.st_dev:
                    raise ValueError("NESTED_STORAGE_MISMATCH")
                with os.scandir(work) as entries:
                    empty = next(entries, None) is None
        boot_id = small_read(Path("/proc/sys/kernel/random/boot_id"))
        if str(UUID(boot_id)) != boot_id or secure_json(config.identity_file) != {
            "owner": self.identity.owner,
            "incarnation": self.identity.incarnation,
            "recovery_generation": self.identity.recovery_generation,
            "boot_id": boot_id,
        }:
            raise ValueError("HOST_BOOT_IDENTITY_MISMATCH")
        with directory(config.execution_cgroup, trusted=True) as group:
            if mount_for(group)["fstype"] != "cgroup2":
                raise ValueError("CGROUP_V2_REQUIRED")
            events = [
                line.split()
                for line in small_read(config.execution_cgroup / "cgroup.events").splitlines()
            ]
            if any(len(line) != 2 for line in events) or len(dict(events)) != len(events):
                raise ValueError("UNKNOWN_TASK_OBSERVATION")
            if dict(events).get("populated") != "0" or dict(events).get("frozen") != "0":
                raise ValueError("HOST_TASKS_ACTIVE")
            group_inode = os.fstat(group).st_ino
        return LinuxSnapshot(boot_id, info.st_dev, info.st_ino, group_inode, empty)

    def bootstrap_approved(self, receipt_ref: str) -> bool:
        config = self.config
        if config.bootstrap_approval is None:
            return False
        return bool(
            secure_json(config.bootstrap_approval)
            == {
                "kind": "empty-node-v1",
                "owner": self.identity.owner,
                "incarnation": self.identity.incarnation,
                "recovery_generation": self.identity.recovery_generation,
                "disk_ref": self.disk_ref,
                "database_ref": self.database_ref,
                "filesystem_uuid": config.filesystem_uuid,
                "database_name": config.database_name,
                "receipt_ref": receipt_ref,
            }
        )
