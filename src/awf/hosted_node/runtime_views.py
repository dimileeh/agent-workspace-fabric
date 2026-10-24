"""Refreshable, workspace-only agent view of already authorized tmpfs material."""

import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.material_store import MaterialStore
from awf.hosted_node.schemas import Identity

AGENT_GID = 1000
_FILES = frozenset({"openai.api-key", "anthropic.api-key", "github.api-key"})
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_SLOT = "0" * 32


class RuntimeViews:
    def __init__(self, store: MaterialStore, *, identity: Identity, agent_gid: int | None = None):
        self.store = store
        self.identity = identity
        self.gid = AGENT_GID if agent_gid is None else agent_gid

    def _key(self, workspace_id: str) -> str:
        if (
            not workspace_id.startswith("ws_")
            or not workspace_id.isascii()
            or not all(c.isalnum() or c in "_-" for c in workspace_id)
        ):
            raise MaterialDeniedError("INVALID_WORKSPACE_ID")
        return hashlib.sha256(
            ("runtime-view:" + self.identity.model_dump_json() + ":" + workspace_id).encode()
        ).hexdigest()[:32]

    def _verify(self, info: os.stat_result, *, directory: bool) -> None:
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not (
            valid_type
            and info.st_uid == os.geteuid()
            and info.st_gid == self.gid
            and stat.S_IMODE(info.st_mode) == (0o710 if directory else 0o440)
            and (directory or info.st_nlink == 1)
        ):
            raise MaterialDeniedError("INVALID_RUNTIME_VIEW")

    def publish(self, workspace_id: str, files: dict[str, bytes]) -> Path:
        if not (set(files) <= _FILES and len(files) == 2 and "github.api-key" in files):
            raise MaterialDeniedError("INVALID_RUNTIME_VIEW_TARGET")
        if any(not 1 <= len(value) <= 65536 for value in files.values()):
            raise MaterialDeniedError("INVALID_RUNTIME_VIEW_MATERIAL")
        key = self._key(workspace_id)
        with self.store._directory(key, _SLOT, create=True) as parent:
            try:
                os.mkdir("view", mode=0o700, dir_fd=parent)
                new = True
            except FileExistsError:
                new = False
            directory = os.open("view", _DIRECTORY, dir_fd=parent)
            try:
                if new:
                    os.fchown(directory, -1, self.gid)
                    os.fchmod(directory, 0o710)
                self._verify(os.fstat(directory), directory=True)
                for name in os.listdir(directory):  # noqa: PTH208 - verified directory fd
                    if name not in files:
                        raise MaterialDeniedError("INVALID_RUNTIME_VIEW_TARGET")
                    self._verify(
                        os.stat(name, dir_fd=directory, follow_symlinks=False), directory=False
                    )
                for name, value in files.items():
                    temporary = name + ".pending"
                    fd = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o400,
                        dir_fd=directory,
                    )
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(value)
                            stream.flush()
                            os.fchown(stream.fileno(), -1, self.gid)
                            os.fchmod(stream.fileno(), 0o440)
                            os.fsync(stream.fileno())
                        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                    finally:
                        with suppress(FileNotFoundError):
                            os.unlink(temporary, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
        return self.store.root / key / "view"

    def remove(self, workspace_id: str) -> None:
        with (
            suppress(FileNotFoundError),
            self.store._directory(self._key(workspace_id), _SLOT, remove_empty=True) as parent,
        ):
            directory = os.open("view", _DIRECTORY, dir_fd=parent)
            try:
                self._verify(os.fstat(directory), directory=True)
                names = os.listdir(directory)  # noqa: PTH208 - verified directory fd
                if not set(names) <= _FILES:
                    raise MaterialDeniedError("INVALID_RUNTIME_VIEW_TARGET")
                for name in names:
                    self._verify(
                        os.stat(name, dir_fd=directory, follow_symlinks=False), directory=False
                    )
                for name in names:
                    os.unlink(name, dir_fd=directory)
            finally:
                os.close(directory)
            os.rmdir("view", dir_fd=parent)  # noqa: PTH106 - verified directory fd
