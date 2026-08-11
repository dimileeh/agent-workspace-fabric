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

from awf.adapters.runtime_executor import AgentRuntimeExecResult
from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR
from awf.node.git_manager import linked_worktree_git_dir, mirror_path_for_worktree

_ISOLATED_REASK_COMMON_GIT_DIR = "/awf-clarification-git-common"


def _copy_regular_git_metadata_file(source_dir: Path, source_name: str, destination: Path) -> None:
    """Copy one linked-worktree control file without following a raced symlink."""
    fds: list[int] = []
    try:
        source_dir_fd = os.open(source_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(source_dir_fd)
        source_fd = os.open(
            source_name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=source_dir_fd,
        )
        fds.append(source_fd)
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise OSError(f"Git metadata source is not a regular file: {source_name}")
        with (
            os.fdopen(source_fd, "rb", closefd=False) as source_file,
            destination.open("xb") as dest_file,
        ):
            shutil.copyfileobj(source_file, dest_file)
    finally:
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)


def _isolated_reask_git_metadata_volume_binds(
    worktree_path: Path,
) -> tuple[tempfile.TemporaryDirectory[str] | None, tuple[tuple[Path, str], ...]]:
    """Build credential-free Git discovery binds for a linked re-ask worktree.

    A linked worktree's ``.git`` file points at metadata beneath its shared
    bare mirror. The clarification container instead receives a detached bare
    clone of its current HEAD, preventing it from reading other worktrees'
    refs or objects. Git needs only selected linked control files and the
    snapshot's common Git directory to recognise the worktree.
    """
    mirror_path = mirror_path_for_worktree(worktree_path)
    linked_git_dir = linked_worktree_git_dir(worktree_path)
    if mirror_path is None or linked_git_dir is None or not linked_git_dir.is_dir():
        return None, ()
    try:
        linked_git_dir.relative_to(mirror_path)
    except ValueError:
        return None, ()
    temporary_metadata: tempfile.TemporaryDirectory[str] | None = None
    try:
        # Docker resolves bind sources on the host, so place the snapshot beside
        # the host-visible mirror/worktree directories rather than in worker /tmp.
        temporary_metadata = tempfile.TemporaryDirectory[str](
            prefix=f".awf-clarification-git-{worktree_path.name}--",
            dir=mirror_path.parent.parent,
        )
        temporary_path = Path(temporary_metadata.name)
        snapshot_path = temporary_path / "linked-git"
        snapshot_path.mkdir()
        common_path = temporary_path / "common-git"
        subprocess.run(
            [
                "git",
                "clone",
                "--bare",
                "--no-local",
                "--no-tags",
                "--single-branch",
                str(worktree_path),
                str(common_path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
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
        _copy_regular_git_metadata_file(linked_git_dir, "HEAD", snapshot_path / "HEAD")
        (snapshot_path / "commondir").write_text(
            f"{_ISOLATED_REASK_COMMON_GIT_DIR}\n", encoding="utf-8"
        )
        (snapshot_path / "gitdir").write_text(f"{DEFAULT_AGENT_WORKDIR}/.git\n", encoding="utf-8")
        try:
            _copy_regular_git_metadata_file(linked_git_dir, "index", snapshot_path / "index")
        except OSError:
            # The index is optional; a raced link, special file, or missing index
            # cannot discard an otherwise safe metadata snapshot.
            pass
        else:
            try:
                shared_index_output = subprocess.run(
                    ["git", "-C", str(worktree_path), "rev-parse", "--shared-index-path"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).stdout.strip()
                shared_index_path = Path(shared_index_output)
                if not shared_index_path.is_absolute():
                    shared_index_path = worktree_path / shared_index_path
                shared_index_path = Path(os.path.normpath(shared_index_path))
                shared_index_relative_path = shared_index_path.relative_to(linked_git_dir)
                if shared_index_relative_path.parent != Path():
                    raise ValueError("shared index is not directly under the linked Git directory")
                _copy_regular_git_metadata_file(
                    linked_git_dir,
                    shared_index_relative_path.name,
                    snapshot_path / shared_index_relative_path.name,
                )
            except (OSError, subprocess.SubprocessError, ValueError):
                # The split-index backing file is optional; retain the regular
                # index snapshot if it cannot be discovered or copied.
                pass
    except (OSError, subprocess.SubprocessError):
        if temporary_metadata is not None:
            temporary_metadata.cleanup()
        return None, ()
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
