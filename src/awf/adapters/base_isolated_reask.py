"""Isolated clarification re-ask helpers shared by the base adapter."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Final

from awf.adapters.runtime_executor import AgentRuntimeExecResult
from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR
from awf.node.git_manager import git_env_without_object_lookup_overrides

_ISOLATED_REASK_COMMON_GIT_DIR = "/awf-clarification-git-common"
_MAX_ISOLATED_REASK_GIT_METADATA_BYTES: Final = 1024 * 1024
_MAX_ISOLATED_REASK_GIT_INDEX_BYTES: Final = 64 * 1024 * 1024
_MAX_ISOLATED_REASK_GIT_OBJECT_FILE_BYTES: Final = 64 * 1024 * 1024
_MAX_ISOLATED_REASK_GIT_OBJECT_SNAPSHOT_BYTES: Final = 256 * 1024 * 1024
# A normal Git object store has at most two directory levels below ``objects``
# (fan-out directory then loose object). These bounds retain room for Git
# extensions while preventing an agent-writable mirror from making an isolated
# re-ask consume unbounded inodes or worker time with empty entries.
_MAX_ISOLATED_REASK_GIT_OBJECT_DIRECTORY_DEPTH: Final = 4
_MAX_ISOLATED_REASK_GIT_OBJECT_SNAPSHOT_ENTRIES: Final = 10_000


def _copy_regular_git_metadata_file(
    source_dir: Path,
    source_name: str,
    destination: Path,
    *,
    max_bytes: int = _MAX_ISOLATED_REASK_GIT_METADATA_BYTES,
) -> int:
    """Copy one linked-worktree control file without following a raced symlink."""
    source_dir_fd = os.open(source_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return _copy_regular_git_metadata_file_from_directory_fd(
            source_dir_fd, source_name, destination, max_bytes=max_bytes
        )
    finally:
        with contextlib.suppress(OSError):
            os.close(source_dir_fd)


def _copy_regular_git_metadata_file_from_directory_fd(
    source_dir_fd: int,
    source_name: str,
    destination: Path,
    *,
    max_bytes: int = _MAX_ISOLATED_REASK_GIT_METADATA_BYTES,
) -> int:
    """Copy one regular Git metadata file from an already-open directory."""
    source_fd: int | None = None
    destination_created = False
    copy_succeeded = False
    try:
        source_fd = os.open(
            source_name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=source_dir_fd,
        )
        file_stat = os.fstat(source_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(f"Git metadata source is not a regular file: {source_name}")
        if file_stat.st_size > max_bytes:
            raise OSError(f"Git metadata source exceeds size limit: {source_name}")
        copied_bytes = 0
        with destination.open("xb") as dest_file:
            destination_created = True
            while True:
                if copied_bytes == max_bytes:
                    if os.read(source_fd, 1):
                        raise OSError(f"Git metadata source exceeds size limit: {source_name}")
                    copy_succeeded = True
                    return copied_bytes
                chunk = os.read(
                    source_fd,
                    min(64 * 1024, max_bytes - copied_bytes),
                )
                if not chunk:
                    copy_succeeded = True
                    return copied_bytes
                dest_file.write(chunk)
                copied_bytes += len(chunk)
    finally:
        if destination_created and not copy_succeeded:
            with contextlib.suppress(OSError):
                destination.unlink()
        if source_fd is not None:
            with contextlib.suppress(OSError):
                os.close(source_fd)


def _isolated_reask_linked_worktree_git_dir(worktree_path: Path) -> Path | None:
    """Read a linked-worktree `.git` pointer without following a raced replacement."""
    try:
        worktree_fd = os.open(
            worktree_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError:
        return None

    git_file_fd: int | None = None
    try:
        git_file_fd = os.open(
            ".git",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=worktree_fd,
        )
        git_file_stat = os.fstat(git_file_fd)
        if (
            not stat.S_ISREG(git_file_stat.st_mode)
            or git_file_stat.st_size > _MAX_ISOLATED_REASK_GIT_METADATA_BYTES
        ):
            return None
        chunks: list[bytes] = []
        copied_bytes = 0
        while copied_bytes < _MAX_ISOLATED_REASK_GIT_METADATA_BYTES:
            chunk = os.read(
                git_file_fd,
                min(64 * 1024, _MAX_ISOLATED_REASK_GIT_METADATA_BYTES - copied_bytes),
            )
            if not chunk:
                break
            chunks.append(chunk)
            copied_bytes += len(chunk)
        if copied_bytes == _MAX_ISOLATED_REASK_GIT_METADATA_BYTES and os.read(git_file_fd, 1):
            return None
        try:
            content = b"".join(chunks).decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
    except OSError:
        return None
    finally:
        if git_file_fd is not None:
            with contextlib.suppress(OSError):
                os.close(git_file_fd)
        with contextlib.suppress(OSError):
            os.close(worktree_fd)

    prefix = "gitdir: "
    if not content.startswith(prefix):
        return None
    git_dir = Path(content.removeprefix(prefix).strip())
    if not git_dir.is_absolute():
        return Path(os.path.normpath(worktree_path / git_dir))
    return git_dir


def _copy_git_object_directory(source: Path, destination: Path) -> None:
    """Copy a bounded Git object snapshot without links or alternates."""
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _copy_git_object_directory_from_fd(
            source_fd,
            destination,
            relative_path=Path(),
            remaining_snapshot_bytes=_MAX_ISOLATED_REASK_GIT_OBJECT_SNAPSHOT_BYTES,
            remaining_snapshot_entries=_MAX_ISOLATED_REASK_GIT_OBJECT_SNAPSHOT_ENTRIES,
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(source_fd)


def _copy_git_object_directory_from_fd(
    source_fd: int,
    destination: Path,
    *,
    relative_path: Path,
    remaining_snapshot_bytes: int,
    remaining_snapshot_entries: int,
) -> tuple[int, int]:
    """Copy regular object-store entries from an already-open directory."""
    destination.mkdir()
    # ``scandir`` streams names instead of constructing an unbounded list. It
    # owns and closes its descriptor, so scan a duplicate of the caller-owned
    # descriptor used below for secure ``openat``-style operations.
    with os.scandir(os.dup(source_fd)) as entries:
        for entry in entries:
            name = entry.name
            remaining_snapshot_entries -= 1
            if remaining_snapshot_entries < 0:
                raise OSError(f"Git object snapshot exceeds entry limit: {name}")
            # A source-mirror alternates file is precisely the untrusted object
            # lookup this snapshot is intended to prevent.
            if relative_path == Path("info") and name == "alternates":
                continue
            entry_stat = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            destination_entry = destination / name
            if stat.S_ISDIR(entry_stat.st_mode):
                if len(relative_path.parts) >= _MAX_ISOLATED_REASK_GIT_OBJECT_DIRECTORY_DEPTH:
                    raise OSError(f"Git object snapshot exceeds directory depth limit: {name}")
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=source_fd,
                )
                try:
                    if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                        raise OSError(f"Git object source is not a directory: {name}")
                    remaining_snapshot_bytes, remaining_snapshot_entries = (
                        _copy_git_object_directory_from_fd(
                            child_fd,
                            destination_entry,
                            relative_path=relative_path / name,
                            remaining_snapshot_bytes=remaining_snapshot_bytes,
                            remaining_snapshot_entries=remaining_snapshot_entries,
                        )
                    )
                finally:
                    with contextlib.suppress(OSError):
                        os.close(child_fd)
            elif stat.S_ISREG(entry_stat.st_mode):
                if entry_stat.st_size > _MAX_ISOLATED_REASK_GIT_OBJECT_FILE_BYTES:
                    raise OSError(f"Git object source exceeds size limit: {name}")
                if entry_stat.st_size > remaining_snapshot_bytes:
                    raise OSError(f"Git object snapshot exceeds total size limit: {name}")
                copied_bytes = _copy_regular_git_metadata_file_from_directory_fd(
                    source_fd,
                    name,
                    destination_entry,
                    max_bytes=min(
                        _MAX_ISOLATED_REASK_GIT_OBJECT_FILE_BYTES,
                        remaining_snapshot_bytes,
                    ),
                )
                remaining_snapshot_bytes -= copied_bytes
            else:
                raise OSError(f"Git object source is not a regular file or directory: {name}")
    return remaining_snapshot_bytes, remaining_snapshot_entries


def _write_isolated_reask_source_config(common_path: Path, expected_ref: str) -> None:
    """Write only the object-format settings required by the snapshot source."""
    if len(expected_ref) == 40:
        common_path.joinpath("config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = true\n",
            encoding="utf-8",
        )
        return
    if len(expected_ref) == 64:
        common_path.joinpath("config").write_text(
            "[core]\n\trepositoryformatversion = 1\n\tbare = true\n"
            "[extensions]\n\tobjectformat = sha256\n",
            encoding="utf-8",
        )
        return
    raise OSError("re-ask ref does not use a supported Git object format")


def _split_index_backing_file_name(index_path: Path, expected_ref: str) -> str | None:
    """Return the split-index backing filename encoded in a copied Git index.

    The index has already been copied from the agent-writable linked Git
    directory. Reading its ``link`` extension avoids executing Git with that
    directory's repository configuration just to discover the backing index.
    """
    object_id_bytes = len(expected_ref) // 2
    if object_id_bytes not in (20, 32):
        return None
    try:
        index = index_path.read_bytes()
    except OSError:
        return None
    contents_end = len(index) - object_id_bytes
    if contents_end < 12 or index[:4] != b"DIRC":
        return None
    version = int.from_bytes(index[4:8], byteorder="big")
    if version not in (2, 3, 4):
        return None
    entry_count = int.from_bytes(index[8:12], byteorder="big")
    offset = 12
    entry_header_size = 42 + object_id_bytes
    for _ in range(entry_count):
        entry_start = offset
        if offset + entry_header_size > contents_end:
            return None
        flags_offset = offset + 40 + object_id_bytes
        flags = int.from_bytes(index[flags_offset : flags_offset + 2], byteorder="big")
        offset += entry_header_size
        if flags & 0x4000:
            if version < 3 or offset + 2 > contents_end:
                return None
            offset += 2
        if version == 4:
            while True:
                if offset >= contents_end:
                    return None
                byte = index[offset]
                offset += 1
                if not byte & 0x80:
                    break
        path_end = index.find(b"\0", offset, contents_end)
        if path_end < 0:
            return None
        if version == 4:
            offset = path_end + 1
        else:
            entry_size = path_end + 1 - entry_start
            offset = entry_start + ((entry_size + 7) & ~7)
        if offset > contents_end:
            return None
    while offset < contents_end:
        if offset + 8 > contents_end:
            return None
        extension_signature = index[offset : offset + 4]
        extension_size = int.from_bytes(index[offset + 4 : offset + 8], byteorder="big")
        offset += 8
        if extension_size > contents_end - offset:
            return None
        if extension_signature == b"link":
            if extension_size < object_id_bytes:
                return None
            return f"sharedindex.{index[offset : offset + object_id_bytes].hex()}"
        offset += extension_size
    return None


def _linked_worktree_common_git_dir(
    snapshot_path: Path,
    linked_git_dir: Path,
    *,
    expected_source_mirror: Path,
) -> Path:
    """Validate the snapshotted common Git directory for a linked worktree."""
    commondir = (snapshot_path / "commondir").read_text(encoding="utf-8").strip()
    if not commondir:
        raise OSError("linked Git commondir is empty")
    common_git_dir = Path(commondir)
    if not common_git_dir.is_absolute():
        common_git_dir = linked_git_dir / common_git_dir
    common_git_dir = Path(os.path.normpath(common_git_dir))
    expected_common_git_dir = Path(os.path.normpath(expected_source_mirror))
    if common_git_dir != expected_common_git_dir:
        raise OSError("linked Git commondir does not match the expected source mirror")
    return common_git_dir


def _isolated_reask_git_metadata_volume_binds(
    worktree_path: Path,
    *,
    expected_ref: str,
    expected_source_mirror: Path | None = None,
) -> tuple[tempfile.TemporaryDirectory[str] | None, tuple[tuple[Path, str], ...]]:
    """Build credential-free Git discovery binds for a linked re-ask worktree.

    A linked worktree's ``.git`` file points at metadata beneath its shared
    bare mirror. The clarification container instead receives a detached bare
    clone pinned to the requested re-ask ref, preventing it from reading other
    worktrees' refs or objects. The pointer must resolve to the validated
    source mirror's expected worktree entry before any metadata or objects are
    copied. Git needs only selected linked control files and the snapshot's
    common Git directory to recognise the worktree.
    """
    linked_git_dir = _isolated_reask_linked_worktree_git_dir(worktree_path)
    if linked_git_dir is None or expected_source_mirror is None:
        return None, ()
    expected_linked_git_dir = Path(
        os.path.normpath(expected_source_mirror / "worktrees" / worktree_path.name)
    )
    if Path(os.path.normpath(linked_git_dir)) != expected_linked_git_dir:
        return None, ()
    try:
        # Snapshot the control files through this descriptor. The linked Git
        # directory is agent-writable, so Git must not read it during clone.
        linked_git_dir_fd = os.open(linked_git_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None, ()
    temporary_metadata: tempfile.TemporaryDirectory[str] | None = None
    try:
        # Docker resolves bind sources on the host, so place the snapshot beside
        # the host-visible mirror/worktree directories rather than in worker /tmp.
        temporary_metadata = tempfile.TemporaryDirectory[str](
            prefix=f".awf-clarification-git-{worktree_path.name}--",
            dir=worktree_path.parent.parent,
        )
        temporary_path = Path(temporary_metadata.name)
        snapshot_path = temporary_path / "linked-git"
        snapshot_path.mkdir()
        common_path = temporary_path / "common-git"
        _copy_regular_git_metadata_file_from_directory_fd(
            linked_git_dir_fd, "HEAD", snapshot_path / "HEAD"
        )
        _copy_regular_git_metadata_file_from_directory_fd(
            linked_git_dir_fd, "commondir", snapshot_path / "commondir"
        )
        common_git_dir = _linked_worktree_common_git_dir(
            snapshot_path,
            linked_git_dir,
            expected_source_mirror=expected_source_mirror,
        )
        (snapshot_path / "commondir").write_text(f"{common_git_dir}\n", encoding="utf-8")
        git_environment = git_env_without_object_lookup_overrides()
        snapshotted_ref = subprocess.run(
            ["git", "--git-dir", str(snapshot_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=git_environment,
        ).stdout.strip()
        if snapshotted_ref != expected_ref:
            raise OSError("linked Git HEAD does not match the requested re-ask ref")
        # Snapshot HEAD may be symbolic. Pin it after validation so a writer
        # to the shared mirror cannot move the selected ref before clone.
        (snapshot_path / "HEAD").write_text(f"{expected_ref}\n", encoding="utf-8")
        source_common_path = temporary_path / "source-common-git"
        source_common_path.mkdir()
        (source_common_path / "refs").mkdir()
        _copy_git_object_directory(common_git_dir / "objects", source_common_path / "objects")
        _write_isolated_reask_source_config(source_common_path, expected_ref)
        with contextlib.suppress(FileNotFoundError):
            _copy_regular_git_metadata_file(
                common_git_dir,
                "shallow",
                source_common_path / "shallow",
            )
        # The bare clone must read objects from the immutable copy above, not
        # through the writable source mirror's common directory.
        (snapshot_path / "commondir").write_text(f"{source_common_path}\n", encoding="utf-8")
        subprocess.run(
            [
                "git",
                "clone",
                "--bare",
                "--no-local",
                "--no-tags",
                "--single-branch",
                str(snapshot_path),
                str(common_path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
            env=git_environment,
        )
        # Retain clone-created core/extensions metadata (notably SHA-256 object
        # format settings), but remove the remote section whose URL may contain
        # credentials. A bare clone always creates this origin section.
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(common_path / "config"),
                "--remove-section",
                "remote.origin",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        (snapshot_path / "commondir").write_text(
            f"{_ISOLATED_REASK_COMMON_GIT_DIR}\n", encoding="utf-8"
        )
        (snapshot_path / "gitdir").write_text(f"{DEFAULT_AGENT_WORKDIR}/.git\n", encoding="utf-8")
        try:
            _copy_regular_git_metadata_file_from_directory_fd(
                linked_git_dir_fd,
                "index",
                snapshot_path / "index",
                max_bytes=_MAX_ISOLATED_REASK_GIT_INDEX_BYTES,
            )
        except OSError:
            # The index is optional; a raced link, special file, or missing index
            # cannot discard an otherwise safe metadata snapshot.
            pass
        else:
            try:
                shared_index_name = _split_index_backing_file_name(
                    snapshot_path / "index", expected_ref
                )
                if shared_index_name is None:
                    raise ValueError("index does not reference a split-index backing file")
                _copy_regular_git_metadata_file_from_directory_fd(
                    linked_git_dir_fd,
                    shared_index_name,
                    snapshot_path / shared_index_name,
                    max_bytes=_MAX_ISOLATED_REASK_GIT_INDEX_BYTES,
                )
            except (OSError, subprocess.SubprocessError, ValueError):
                # The split-index backing file is optional; retain the regular
                # index snapshot if it cannot be discovered or copied.
                pass
    except (OSError, subprocess.SubprocessError):
        if temporary_metadata is not None:
            temporary_metadata.cleanup()
        return None, ()
    finally:
        with contextlib.suppress(OSError):
            os.close(linked_git_dir_fd)
    return temporary_metadata, (
        (snapshot_path, str(linked_git_dir)),
        (common_path, _ISOLATED_REASK_COMMON_GIT_DIR),
    )


def _discard_isolated_reask_git_metadata_task_result(
    task: asyncio.Task[
        tuple[tempfile.TemporaryDirectory[str] | None, tuple[tuple[Path, str], ...]]
    ],
) -> None:
    """Consume a cancelled re-ask snapshot task and remove its temporary metadata."""
    try:
        temporary_metadata, _volume_binds = task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        return
    if temporary_metadata is not None:
        with contextlib.suppress(OSError):
            temporary_metadata.cleanup()


def _discard_hosted_execute_task_result(task: asyncio.Task[AgentRuntimeExecResult]) -> None:
    """Consume a cancelled hosted-execution task's eventual result."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
