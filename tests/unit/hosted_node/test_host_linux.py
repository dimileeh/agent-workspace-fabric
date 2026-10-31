import json
import os
import stat
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.hosted_node import host_linux as linux
from awf.hosted_node.schemas import Identity
from tests.unit.hosted_node.test_host_config import host_config

IDENTITY = Identity(
    owner="org/node", incarnation="8063738394745155541", recovery_generation=1, boot_epoch=2
)
BOOT = "c89870e2-1c61-49b4-a9a9-2277ad31cb52"


@pytest.fixture
def synthetic_linux(monkeypatch):
    # Patching linux.os.fstat directly would also replace pytest's os.fstat.
    monkeypatch.setattr(linux, "os", SimpleNamespace(**vars(os)))
    config = host_config()
    device = os.makedev(8, 1)
    info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700, st_dev=device, st_ino=7)
    mount = {
        "fstype": "ext4",
        "root": "/",
        "target": str(config.state_root),
        "device": device,
        "options": {"rw"},
    }
    files = {
        config.state_root / ".awf-node.json": {
            "owner": IDENTITY.owner,
            "disk_ref": "disk",
            "database_ref": "db",
            "filesystem_uuid": config.filesystem_uuid,
        },
        config.identity_file: {
            "owner": IDENTITY.owner,
            "incarnation": IDENTITY.incarnation,
            "recovery_generation": 1,
            "boot_id": BOOT,
        },
        config.bootstrap_approval: {
            "kind": "empty-node-v1",
            "owner": IDENTITY.owner,
            "incarnation": IDENTITY.incarnation,
            "recovery_generation": 1,
            "disk_ref": "disk",
            "database_ref": "db",
            "filesystem_uuid": config.filesystem_uuid,
            "database_name": "node_db",
            "receipt_ref": "first-node",
        },
    }

    @contextmanager
    def directory(path, **kwargs):
        yield 2 if path == config.execution_cgroup else 1

    monkeypatch.setattr(linux, "directory", directory)
    monkeypatch.setattr(linux.os, "fstat", lambda _: info)
    monkeypatch.setattr(
        linux, "mount_for", lambda fd: {**mount, "fstype": "cgroup2"} if fd == 2 else mount
    )
    monkeypatch.setattr(linux, "secure_json", lambda path: files[path])
    monkeypatch.setattr(linux, "block_device", lambda _: device)
    monkeypatch.setattr(
        linux,
        "small_read",
        lambda path: BOOT if "boot_id" in str(path) else "populated 0\nfrozen 0\n",
    )
    monkeypatch.setattr(linux.os, "scandir", lambda _: nullcontext(iter(())))
    monkeypatch.setattr(linux.sys, "platform", "linux")
    return linux.LinuxObservation(config, IDENTITY, "disk", "db"), files, info, mount


def test_linux_device_boot_cgroup_and_operator_first_node(synthetic_linux):
    probe, _, _, _ = synthetic_linux
    value = probe.sample()
    assert value.boot_id == BOOT and value.workspaces_empty
    assert probe.bootstrap_approved("first-node")
    assert not probe.bootstrap_approved("caller-key")


def test_synthetic_linux_preserves_unrelated_filesystem_metadata(synthetic_linux, tmp_path):
    path = tmp_path / "unrelated"
    path.write_bytes(b"real metadata")
    expected = path.stat()
    with path.open("rb") as stream:
        actual = os.fstat(stream.fileno())
        assert (actual.st_size, actual.st_dev, actual.st_ino) == (
            expected.st_size,
            expected.st_dev,
            expected.st_ino,
        )
    with os.scandir(tmp_path) as entries:
        assert [entry.name for entry in entries] == [path.name]
    probe, _, _, _ = synthetic_linux
    assert probe.sample().boot_id == BOOT


@pytest.mark.parametrize("has_entries", [False, True])
def test_retained_directory_probe_reads_at_most_one_entry(
    synthetic_linux, monkeypatch, has_entries
):
    probe, _, _, _ = synthetic_linux
    reads = 0
    closed = False

    class Entries:
        def __iter__(self):
            return self

        def __next__(self):
            nonlocal reads
            reads += 1
            assert reads == 1, "the emptiness probe must not enumerate retained worktrees"
            if not has_entries:
                raise StopIteration
            return object()

    @contextmanager
    def scan(fd):
        nonlocal closed
        assert fd == 1
        try:
            yield Entries()
        finally:
            closed = True

    monkeypatch.setattr(linux.os, "scandir", scan)
    monkeypatch.setattr(
        linux.os, "listdir", lambda _: pytest.fail("retained directories must not be materialized")
    )
    assert probe.sample().workspaces_empty is not has_entries
    assert reads == 1 and closed


@pytest.mark.parametrize(
    "case",
    [
        "platform",
        "device",
        "mount",
        "bind_subdir",
        "read_only",
        "marker",
        "physical",
        "boot",
        "cgroup",
        "cgroup_fs",
        "nested_mount",
    ],
)
def test_unknown_linux_identity_or_activity_fails_closed(synthetic_linux, monkeypatch, case):
    probe, files, info, mount = synthetic_linux
    if case == "platform":
        monkeypatch.setattr(linux.sys, "platform", "darwin")
    elif case == "device":
        monkeypatch.setattr(linux, "block_device", lambda _: 42)
    elif case == "mount":
        mount["target"] = "/"
    elif case == "bind_subdir":
        mount["root"] = "/wrong-subtree"
    elif case == "read_only":
        mount["options"] = {"ro"}
    elif case == "marker":
        files[probe.config.state_root / ".awf-node.json"]["database_ref"] = "foreign"
    elif case == "physical":
        files[probe.config.identity_file]["incarnation"] = "123"
    elif case == "boot":
        files[probe.config.identity_file]["boot_id"] = "old"
    elif case == "cgroup":
        monkeypatch.setattr(
            linux,
            "small_read",
            lambda path: BOOT if "boot_id" in str(path) else "populated 1\nfrozen 0\n",
        )
    elif case == "cgroup_fs":
        monkeypatch.setattr(linux, "mount_for", lambda _: mount)
    else:
        calls = 0

        def fstat(_):
            nonlocal calls
            calls += 1
            return info if calls == 1 else SimpleNamespace(st_dev=999, st_ino=7)

        monkeypatch.setattr(linux.os, "fstat", fstat)
    with pytest.raises(ValueError):
        probe.sample()


def test_bounded_private_file_rejects_symlink_mode_owner_and_json(tmp_path, monkeypatch):
    monkeypatch.setattr(linux, "os", SimpleNamespace(**vars(os)))
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"known": "metadata"}))
    path.chmod(0o400)
    original = linux.os.fstat

    def owned(fd):
        info = original(fd)
        return SimpleNamespace(
            st_mode=info.st_mode & ~0o022 if stat.S_ISDIR(info.st_mode) else info.st_mode,
            st_uid=0,
            st_nlink=info.st_nlink,
            st_size=info.st_size,
        )

    monkeypatch.setattr(linux.os, "fstat", owned)
    assert linux.secure_json(path) == {"known": "metadata"}
    path.chmod(0o666)
    with pytest.raises(ValueError):
        linux.secure_json(path)
    path.chmod(0o400)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        linux.secure_json(link)
    monkeypatch.setattr(linux.os, "fstat", original)
    if os.getuid() != 0:
        with pytest.raises(ValueError):
            linux.secure_json(path)


def test_mountinfo_parser_uses_open_descriptor_mount_id(monkeypatch):
    def read(path, **kwargs):
        if "fdinfo" in str(path):
            return "pos: 0\nmnt_id: 36\n"
        return "35 1 8:2 / /other rw - xfs /dev/sdb rw\n36 1 8:1 / /var/lib/awf rw - ext4 /dev/sda rw\n"

    monkeypatch.setattr(linux, "small_read", read)
    assert linux.mount_for(123)["device"] == os.makedev(8, 1)
    assert linux.mount_for(123)["target"] == "/var/lib/awf"


def test_trusted_metadata_cannot_have_writable_or_unowned_ancestor(tmp_path, monkeypatch):
    monkeypatch.setattr(linux, "os", SimpleNamespace(**vars(os)))
    path = tmp_path / "receipt.json"
    path.write_text("{}")
    path.chmod(0o444)
    real = linux.os.fstat

    def unsafe_parent(fd):
        value = real(fd)
        return SimpleNamespace(
            st_mode=value.st_mode | (0o002 if stat.S_ISDIR(value.st_mode) else 0),
            st_uid=0,
            st_nlink=value.st_nlink,
            st_size=value.st_size,
        )

    monkeypatch.setattr(linux.os, "fstat", unsafe_parent)
    with pytest.raises(ValueError, match="TRUSTED_HOST_DIRECTORY_REQUIRED"):
        linux.secure_json(path)


@pytest.mark.parametrize(
    "events", ["populated 0\npopulated 0\nfrozen 0", "bad", "populated 0\nfrozen 1", "populated 0"]
)
def test_malformed_or_frozen_cgroup_is_unknown(synthetic_linux, monkeypatch, events):
    probe, _, _, _ = synthetic_linux
    monkeypatch.setattr(
        linux, "small_read", lambda path: BOOT if "boot_id" in str(path) else events
    )
    with pytest.raises(ValueError):
        probe.sample()


def test_device_must_be_block_special_and_reads_are_bounded(tmp_path):
    path = tmp_path / "file"
    path.write_text("abcdefghijk")
    with pytest.raises(ValueError, match="RETAINED_BLOCK_DEVICE_REQUIRED"):
        linux.block_device(path)
    with pytest.raises(ValueError, match="HOST_OBSERVATION_TOO_LARGE"):
        linux.small_read(path, limit=2)
    assert linux.small_read(path) == "abcdefghijk"


def test_cgroup_and_state_directory_must_be_root_owned(synthetic_linux):
    probe, _, info, _ = synthetic_linux
    info.st_uid = 1234
    with pytest.raises(ValueError):
        probe.sample()


def test_untrusted_path_and_real_directory_descriptor(tmp_path):
    with pytest.raises(ValueError), linux.directory(Path("relative")):
        pytest.fail("relative host path")
    with linux.directory(tmp_path) as fd:
        assert os.fstat(fd).st_ino == tmp_path.stat().st_ino


def test_real_block_device_stat_and_no_bootstrap_approval(monkeypatch):
    fake_path = SimpleNamespace(stat=lambda: SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=123))
    assert linux.block_device(fake_path) == 123
    probe = linux.LinuxObservation(host_config(bootstrap_approval=None), IDENTITY, "disk", "db")
    assert not probe.bootstrap_approved("caller-input")


def test_metadata_growth_after_stat_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(linux, "os", SimpleNamespace(**vars(os)))
    path = tmp_path / "receipt.json"
    path.write_bytes(b" " * 8193)
    path.chmod(0o444)
    real = linux.os.fstat

    def owned_with_old_size(fd):
        value = real(fd)
        return SimpleNamespace(
            st_uid=0,
            st_mode=value.st_mode & ~0o022 if stat.S_ISDIR(value.st_mode) else value.st_mode,
            st_nlink=value.st_nlink,
            st_size=1,
        )

    monkeypatch.setattr(linux.os, "fstat", owned_with_old_size)
    with pytest.raises(ValueError, match="HOST_OBSERVATION_TOO_LARGE"):
        linux.secure_json(path)
