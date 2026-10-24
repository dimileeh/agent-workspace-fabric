import tempfile
from pathlib import Path

import pytest

from awf.hosted_node.material_store import MaterialStore


def test_atomic_private_files_and_attacks():
    with tempfile.TemporaryDirectory(dir="/dev/shm") as root:
        store = MaterialStore(Path(root))
        slot, workspace = "a" * 32, "b" * 32
        path = store.install(workspace, slot, b"generated-test-material")
        assert path.read_bytes() == b"generated-test-material"
        assert path.stat().st_mode & 0o777 == 0o400
        assert store.exists(workspace, slot)
        path.chmod(0o600)
        assert not store.exists(workspace, slot)
        store.remove(workspace, slot)
        path.parent.mkdir(mode=0o700)
        path.symlink_to("/etc/passwd")
        assert not store.exists(workspace, slot)
        store.remove(workspace, slot)
        assert not path.is_symlink()
        for unsafe in ("../escape", "/tmp/foo", "a/b"):
            with pytest.raises(ValueError):
                store.install(workspace, unsafe, b"a")
        store.close()


def test_root_must_be_private_ephemeral_and_not_symlink(tmp_path):
    with pytest.raises(ValueError):
        MaterialStore(tmp_path)
    with tempfile.TemporaryDirectory(dir="/dev/shm") as root:
        link = Path(root) / "link"
        link.symlink_to(root)
        with pytest.raises((ValueError, OSError)):
            MaterialStore(link)
        Path(root).chmod(0o755)
        with pytest.raises(ValueError):
            MaterialStore(Path(root))


def test_hardlinks_symlink_directories_and_root_replacement():
    with tempfile.TemporaryDirectory(dir="/dev/shm") as root:
        store = MaterialStore(Path(root))
        slot, workspace = "a" * 32, "b" * 32
        path = store.install(workspace, slot, b"synthetic")
        alias = path.with_name("alias")
        alias.hardlink_to(path)
        assert not store.exists(workspace, slot)
        alias.unlink()
        assert store.exists(workspace, slot)
        moved = path.parent.with_name("moved")
        path.parent.rename(moved)
        path.parent.symlink_to(moved, target_is_directory=True)
        assert not store.exists(workspace, slot)
        with pytest.raises(OSError):
            store.install(workspace, "c" * 32, b"synthetic")
        path.parent.unlink()
        moved.rename(path.parent)
        assert store.exists(workspace, slot)
        store.close()
        assert not store.exists(workspace, slot)
        with pytest.raises(ValueError):
            store.install(workspace, slot, b"synthetic")


def test_install_is_no_replace_and_size_bounded():
    with tempfile.TemporaryDirectory(dir="/dev/shm") as root:
        store = MaterialStore(Path(root))
        slot, workspace = "a" * 32, "b" * 32
        for material in (b"", b"x" * 65537):
            with pytest.raises(ValueError):
                store.install(workspace, slot, material)
        path = store.install(workspace, slot, b"first")
        with pytest.raises(FileExistsError):
            store.install(workspace, slot, b"replacement")
        assert path.read_bytes() == b"first"
        assert not list(path.parent.glob("*.pending"))
        store.remove(workspace, "c" * 32)
        store.remove("c" * 32, slot)
        store.close()


def test_root_replacement_and_fifo_are_denied():
    import os

    with tempfile.TemporaryDirectory(dir="/dev/shm") as root:
        root_path = Path(root)
        store = MaterialStore(root_path)
        slot, workspace = "a" * 32, "b" * 32
        path = store.install(workspace, slot, b"synthetic")
        path.unlink()
        os.mkfifo(path, 0o400)
        assert not store.exists(workspace, slot)
        store.remove(workspace, slot)
        old = root_path.with_name(root_path.name + "-old")
        root_path.rename(old)
        root_path.mkdir(mode=0o700)
        try:
            assert not store.exists(workspace, slot)
            with pytest.raises(ValueError, match="ROOT_REPLACED"):
                store.install(workspace, slot, b"synthetic")
        finally:
            root_path.rmdir()
            old.rename(root_path)
            store.close()


@pytest.mark.parametrize("boundary", ["staged", "published"])
def test_interrupted_publication_owned_cleanup(interrupt_publication, boundary):
    with tempfile.TemporaryDirectory(dir="/dev/shm") as root:
        store = MaterialStore(Path(root))
        workspace, slot, winner = "a" * 32, "b" * 32, "c" * 32
        winning_path = store.install(workspace, winner, b"winning-version")
        with interrupt_publication(boundary), pytest.raises(OSError):
            store.install(workspace, slot, b"interrupted-version")
        pending = store.path(workspace, slot).with_suffix(".pending")
        assert pending.is_file()
        assert pending.stat().st_mode & 0o777 == 0o400
        assert store.path(workspace, slot).exists() == (boundary == "published")
        assert not store.exists(workspace, slot)
        store.remove(workspace, slot)
        store.remove(workspace, slot)
        assert not pending.exists()
        assert not store.path(workspace, slot).exists()
        assert store.exists(workspace, winner)
        assert winning_path.read_bytes() == b"winning-version"
        store.close()


def test_remove_reclaims_capacity_beyond_preparation_limit():
    with tempfile.TemporaryDirectory(dir="/dev/shm") as root:
        store = MaterialStore(Path(root))
        slot = "a" * 32
        for number in range(1025):
            workspace = f"{number:032x}"
            store.install(workspace, slot, b"synthetic")
            store.remove(workspace, slot)
        assert list(Path(root).iterdir()) == []


def test_remove_empty_directory_under_root_lock(monkeypatch):
    import fcntl
    import os

    with tempfile.TemporaryDirectory(dir="/dev/shm") as root:
        root_path = Path(root)
        store = MaterialStore(root_path)
        workspace, slot = "a" * 32, "b" * 32
        path = store.install(workspace, slot, b"synthetic")
        unknown = path.with_name("unknown")
        unknown.write_bytes(b"preserve")
        store.remove(workspace, slot)
        assert unknown.read_bytes() == b"preserve"
        unknown.unlink()
        pending = path.with_suffix(".pending")
        pending.write_bytes(b"interrupted")
        store.close()
        store = MaterialStore(root_path)
        original_rmdir = os.rmdir
        removed = []

        def locked_rmdir(name, *, dir_fd=None):
            assert name == workspace
            assert dir_fd is not None
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            original_rmdir(name, dir_fd=dir_fd)
            removed.append(name)

        with monkeypatch.context() as patch:
            patch.setattr(os, "rmdir", locked_rmdir)
            store.remove(workspace, slot)
            store.remove(workspace, slot)
        assert removed == [workspace]
        assert not path.parent.exists()
