"""Refreshable, workspace-only agent view of already authorized tmpfs material."""

import asyncio
import hashlib
import os
import stat
from collections.abc import Collection, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4
from weakref import WeakKeyDictionary, WeakValueDictionary

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.material_store import MaterialStore
from awf.hosted_node.schemas import Identity

AGENT_GID = 1000
_FILES = frozenset({"openai.api-key", "anthropic.api-key", "github.api-key"})
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_SLOT = "0" * 32
_PUBLICATION = "publication"


@dataclass(frozen=True)
class PublishedView:
    path: Path
    token: str
    retained_directory: bool = False
    owner: "PublicationOwnership | None" = field(default=None, repr=False, compare=False)


@dataclass(eq=False)
class PublicationOwnership:
    views: "RuntimeViews"
    workspace_id: str
    publication: PublishedView | None = None
    closing: bool = False


@dataclass(eq=False)
class AbsentPublication:
    key: str


_absence_tokens: WeakKeyDictionary[MaterialStore, WeakValueDictionary[str, AbsentPublication]] = (
    WeakKeyDictionary()
)


_publication_ownership: ContextVar[PublicationOwnership | None] = ContextVar(
    "hosted_publication_ownership", default=None
)


def owned_publication(views: "RuntimeViews", workspace_id: str) -> PublicationOwnership | None:
    value = _publication_ownership.get()
    return (
        value
        if value is not None and value.views is views and value.workspace_id == workspace_id
        else None
    )


@contextmanager
def track_publications(views: "RuntimeViews", workspace_id: str) -> Iterator[None]:
    token = _publication_ownership.set(PublicationOwnership(views, workspace_id))
    try:
        yield
    finally:
        _publication_ownership.reset(token)


class RuntimeViews:
    def __init__(self, store: MaterialStore, *, identity: Identity, agent_gid: int | None = None):
        self.store = store
        self.identity = identity
        self.gid = AGENT_GID if agent_gid is None else agent_gid
        self._owners: dict[str, PublicationOwnership] = {}
        self._writers: set[asyncio.Task[PublishedView | None]] = set()
        self._closed = False
        self._absence_tokens = _absence_tokens.setdefault(store, WeakValueDictionary())

    @property
    def closed(self) -> bool:
        return self._closed

    def close_admission(self) -> None:
        with self.store._locked_root():
            self._closed = True

    def track_writer(self, task: asyncio.Task[PublishedView | None]) -> None:
        self._writers.add(task)
        task.add_done_callback(self._writers.discard)

    async def drain_writers(self) -> None:
        while self._writers:
            pending = tuple(self._writers)
            await asyncio.gather(*pending, return_exceptions=True)
            self._writers.difference_update(pending)

    def clear_owned(self) -> None:
        with self.store._locked_root():
            recorded = [(key, owner.publication) for key, owner in self._owners.items()]
        errors: list[Exception] = []
        for workspace_id, publication in recorded:
            if publication is not None:
                try:
                    self.remove_owned(workspace_id, publication)
                except Exception as error:  # noqa: BLE001 - finish other owned retractions
                    errors.append(error)
        if errors:
            raise ExceptionGroup("Runtime view retraction failed", errors)

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

    def _view_files(self, directory: int, targets: Collection[str]) -> list[str]:
        names = os.listdir(directory)  # noqa: PTH208 - verified directory fd
        pending = {name + ".pending" for name in targets}
        for name in names:
            if name not in targets and name not in pending:
                raise MaterialDeniedError("INVALID_RUNTIME_VIEW_TARGET")
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if name in targets:
                self._verify(info, directory=False)
            elif not (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and info.st_nlink == 1
                and 0 <= info.st_size <= 65536
                and (stat.S_IMODE(info.st_mode), info.st_gid)
                in {(0o400, os.getegid()), (0o400, self.gid), (0o440, self.gid)}
            ):
                raise MaterialDeniedError("INVALID_RUNTIME_VIEW")
        return names

    def _view_directory(self, directory: int) -> None:
        info = os.fstat(directory)
        # mkdir/chown can precede a crash. Only an empty, owner-private view
        # can be an unfinished setup; no material was written before chmod.
        if (
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_gid in {os.getegid(), self.gid}
            and stat.S_IMODE(info.st_mode) == 0o700
            and not os.listdir(directory)  # noqa: PTH208 - verified directory fd
        ):
            os.fchown(directory, -1, self.gid)
            os.fchmod(directory, 0o710)
        self._verify(os.fstat(directory), directory=True)

    def _publication(self, parent: int, token: str | None = None) -> str | None:
        flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        flags |= os.O_RDONLY if token is None else os.O_RDWR | os.O_CREAT
        try:
            fd = os.open(_PUBLICATION, flags, 0o600, dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if not (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
            ):
                raise MaterialDeniedError("INVALID_RUNTIME_VIEW")
            if token is not None:
                with os.fdopen(os.dup(fd), "wb") as stream:
                    stream.write(token.encode("ascii"))
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
                return token
            value = os.read(fd, 33)
            if len(value) != 32 or any(byte not in b"0123456789abcdef" for byte in value):
                raise MaterialDeniedError("INVALID_RUNTIME_VIEW")
            return value.decode("ascii")
        finally:
            os.close(fd)

    def absence_token(self, workspace_id: str) -> AbsentPublication | None:
        key = self._key(workspace_id)
        with self.store._locked_root() as root:
            if self._closed or workspace_id in self._owners or key in os.listdir(root):  # noqa: PTH208 - locked root fd
                return None
            token = self._absence_tokens.get(key)
            if token is None:
                token = AbsentPublication(key)
                self._absence_tokens[key] = token
            return token

    def publish(
        self,
        workspace_id: str,
        files: dict[str, bytes],
        *,
        expected_absence: AbsentPublication | None = None,
    ) -> PublishedView | None:
        return self._publish(workspace_id, files, expected_absence=expected_absence)

    def publication(self, workspace_id: str) -> PublishedView | None:
        key = self._key(workspace_id)
        with suppress(FileNotFoundError), self.store._directory(key, _SLOT) as parent:
            token = self._publication(parent)
            if token is not None:
                owner = self._owners.get(workspace_id)
                if (
                    owner is not None
                    and owner.publication is not None
                    and owner.publication.token == token
                ):
                    return owner.publication
                return PublishedView(self.store.root / key / "view", token)
        return None

    def refresh(
        self, workspace_id: str, files: dict[str, bytes], publication: PublishedView
    ) -> PublishedView | None:
        return self._publish(workspace_id, files, publication=publication)

    def _publish(
        self,
        workspace_id: str,
        files: dict[str, bytes],
        *,
        publication: PublishedView | None = None,
        expected_absence: AbsentPublication | None = None,
    ) -> PublishedView | None:
        if not (set(files) <= _FILES and len(files) == 2 and "github.api-key" in files):
            raise MaterialDeniedError("INVALID_RUNTIME_VIEW_TARGET")
        if any(not 1 <= len(value) <= 65536 for value in files.values()):
            raise MaterialDeniedError("INVALID_RUNTIME_VIEW_MATERIAL")
        key = self._key(workspace_id)
        if publication is not None and publication.path != self.store.root / key / "view":
            raise MaterialDeniedError("INVALID_RUNTIME_VIEW")
        token = uuid4().hex
        failure: BaseException | None = None
        result: PublishedView | None = None
        with self.store._directory(key, _SLOT, create=True, remove_empty=True) as parent:
            if self._closed:
                # Shutdown fences publication, not still-valid authority. A
                # queued final check may finish without recreating any view.
                return None
            if expected_absence is not None and (
                self._absence_tokens.get(key) is not expected_absence
                or workspace_id in self._owners
                or os.listdir(parent)  # noqa: PTH208 - locked directory fd
            ):
                return None
            ownership = owned_publication(self, workspace_id)
            if ownership is not None and ownership.closing:
                # A queued writer may reach this lock only after scope closure.
                # It may refresh its own publication, never adopt a successor.
                if ownership.publication is None or (
                    publication is not None and publication != ownership.publication
                ):
                    return None
                publication = ownership.publication
            # A retained sweep must not resurrect a cleared view or replace a
            # consumer's newer publication after its authority reads yielded.
            if publication is not None and self._publication(parent) != publication.token:
                return None
            if publication is not None:
                if publication.owner is None or publication.owner is not self._owners.get(
                    workspace_id
                ):
                    return None
                ownership = publication.owner
            if ownership is None:
                ownership = PublicationOwnership(self, workspace_id)
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
                self._view_directory(directory)
                # The root flock proves any prior writer is gone. Validate all
                # entries before removing only its bounded private remnants.
                for name in self._view_files(directory, files):
                    if name not in files:
                        os.unlink(name, dir_fd=directory)
                try:
                    # The private token and file replacements share the root lock.
                    # Identical refreshed bytes still constitute a new publication.
                    result = PublishedView(
                        self.store.root / key / "view",
                        token,
                        retained_directory=not new,
                        owner=ownership,
                    )
                    # Only this workspace's in-flight absence readers are fenced;
                    # a later clear cannot make their old token valid again.
                    self._absence_tokens.pop(key, None)
                    ownership.publication = result
                    self._owners[workspace_id] = ownership
                    self._publication(parent, token)
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
                except BaseException as error:
                    # No other publisher can interleave with partial-write cleanup.
                    self._remove_view(parent)
                    self._owners.pop(workspace_id, None)
                    failure = error
            finally:
                os.close(directory)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def _remove_view(self, parent: int, *, preserve_directory: bool = False) -> None:
        directory = os.open("view", _DIRECTORY, dir_fd=parent)
        try:
            self._view_directory(directory)
            names = self._view_files(directory, _FILES)
            for name in names:
                os.unlink(name, dir_fd=directory)
        finally:
            os.close(directory)
        if not preserve_directory:
            os.rmdir("view", dir_fd=parent)  # noqa: PTH106 - verified directory fd
        with suppress(FileNotFoundError):
            os.unlink(_PUBLICATION, dir_fd=parent)

    def remove(
        self,
        workspace_id: str,
        *,
        publication: PublishedView | None = None,
        preserve_directory: bool = False,
    ) -> None:
        key = self._key(workspace_id)
        if publication is not None and publication.path != self.store.root / key / "view":
            raise MaterialDeniedError("INVALID_RUNTIME_VIEW")
        if publication is None:
            # Fence absent readers before directory lookup, without nesting root flocks.
            with self.store._locked_root():
                self._absence_tokens.pop(key, None)
        with (
            suppress(FileNotFoundError),
            self.store._directory(key, _SLOT, remove_empty=True) as parent,
        ):
            if publication is not None and self._publication(parent) != publication.token:
                return
            self._absence_tokens.pop(key, None)
            self._remove_view(parent, preserve_directory=preserve_directory)
            self._owners.pop(workspace_id, None)

    def remove_owned(self, workspace_id: str, publication: PublishedView) -> None:
        key = self._key(workspace_id)
        if publication.path != self.store.root / key / "view":
            raise MaterialDeniedError("INVALID_RUNTIME_VIEW")
        with suppress(FileNotFoundError), self.store._directory(key, _SLOT) as parent:
            owner = self._owners.get(workspace_id)
            current = owner.publication if owner is not None else None
            if (
                owner is not publication.owner
                or current is None
                or self._publication(parent) != current.token
            ):
                return
            self._absence_tokens.pop(key, None)
            self._remove_view(parent, preserve_directory=True)
            self._owners.pop(workspace_id, None)

    def retire_empty(self, workspace_id: str, *, current: Identity) -> bool:
        """Reclaim a receipt-bound retired boot, never a warm/current publication."""
        if self.identity.owner != current.owner or self.identity.boot_epoch >= current.boot_epoch:
            return False
        key = self._key(workspace_id)
        with (
            suppress(FileNotFoundError),
            self.store._directory(key, _SLOT, remove_empty=True) as parent,
        ):
            # A token, material, or unknown entry is not proof of retirement.
            if set(os.listdir(parent)) != {"view"}:  # noqa: PTH208 - locked directory fd
                return False
            directory = os.open("view", _DIRECTORY, dir_fd=parent)
            try:
                self._view_directory(directory)
                if os.listdir(directory):  # noqa: PTH208 - verified directory fd
                    return False
            finally:
                os.close(directory)
            os.rmdir("view", dir_fd=parent)  # noqa: PTH106 - verified directory fd
            return True
        return False
