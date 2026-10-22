"""Linux tmpfs-only, descriptor-relative, bounded private material files."""

import fcntl
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from awf.hosted_node.identity import MaterialDeniedError

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_SLOT = re.compile(r"^[0-9a-f]{32}$")


class MaterialStore:
    def __init__(self, root: Path):
        self.root = root
        self.closed = False
        fd = self._open_root()
        try:
            info = os.fstat(fd)
            self._device_inode = (info.st_dev, info.st_ino)
            mount_id = next(
                line.split()[1]
                for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
                if line.startswith("mnt_id:")
            )
            mount = next(
                line
                for line in Path("/proc/self/mountinfo").read_text().splitlines()
                if line.split()[0] == mount_id
            )
            if mount.split(" - ")[1].split()[0] != "tmpfs":
                raise MaterialDeniedError("EPHEMERAL_TMPFS_REQUIRED")
        finally:
            os.close(fd)

    def _open_root(self) -> int:
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise MaterialDeniedError("INVALID_MATERIAL_ROOT")
        fd = os.open("/", _DIRECTORY)
        try:
            for component in self.root.parts[1:]:
                child = os.open(component, _DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise MaterialDeniedError("PRIVATE_ROOT_REQUIRED")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _directory(self, workspace: str, slot: str, *, create: bool = False) -> Iterator[int]:
        if self.closed or not _SLOT.fullmatch(workspace) or not _SLOT.fullmatch(slot):
            raise MaterialDeniedError("INVALID_FILE_SLOT")
        root = self._open_root()
        try:
            info = os.fstat(root)
            if (info.st_dev, info.st_ino) != self._device_inode:
                raise MaterialDeniedError("ROOT_REPLACED")
            fcntl.flock(root, fcntl.LOCK_EX)
            if create:
                entries = os.listdir(root)  # noqa: PTH208 - descriptor-relative security boundary
                if len(entries) >= 1024 and workspace not in entries:
                    raise MaterialDeniedError("MATERIAL_CAPACITY", 503)
                with suppress(FileExistsError):
                    os.mkdir(workspace, mode=0o700, dir_fd=root)
            directory = os.open(workspace, _DIRECTORY, dir_fd=root)
            try:
                info = os.fstat(directory)
                if (
                    info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_dev != self._device_inode[0]
                ):
                    raise MaterialDeniedError("INVALID_WORKSPACE_DIRECTORY")
                if create and len(os.listdir(directory)) >= 128:  # noqa: PTH208 - directory fd
                    raise MaterialDeniedError("MATERIAL_CAPACITY", 503)
                yield directory
            finally:
                os.close(directory)
        finally:
            os.close(root)

    def install(self, workspace: str, slot: str, material: bytes) -> Path:
        if not 1 <= len(material) <= 65536:
            raise MaterialDeniedError("INVALID_MATERIAL", 422)
        with self._directory(workspace, slot, create=True) as directory:
            temporary = slot + ".pending"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o400,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(material)
                    stream.flush()
                    os.fsync(stream.fileno())
                # link is an atomic, no-replacement publication; private temp is then removed.
                os.link(
                    temporary,
                    slot + ".material",
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                os.fsync(directory)
            finally:
                os.unlink(temporary, dir_fd=directory)
        return self.path(workspace, slot)

    def path(self, workspace: str, slot: str) -> Path:
        return self.root / workspace / (slot + ".material")

    def exists(self, workspace: str, slot: str) -> bool:
        try:
            with self._directory(workspace, slot) as directory:
                fd = os.open(
                    slot + ".material",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
                try:
                    info = os.fstat(fd)
                    return (
                        stat.S_ISREG(info.st_mode)
                        and info.st_nlink == 1
                        and info.st_uid == os.geteuid()
                        and stat.S_IMODE(info.st_mode) == 0o400
                        and 1 <= info.st_size <= 65536
                    )
                finally:
                    os.close(fd)
        except (OSError, MaterialDeniedError):
            return False

    def remove(self, workspace: str, slot: str) -> None:
        try:
            with self._directory(workspace, slot) as directory:
                for suffix in (".material", ".pending"):
                    with suppress(FileNotFoundError):
                        os.unlink(slot + suffix, dir_fd=directory)
                os.fsync(directory)
        except FileNotFoundError:
            pass

    def close(self) -> None:
        self.closed = True
