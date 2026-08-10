"""Isolated clarification re-ask helpers shared by the base adapter."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from awf.adapters.runtime_executor import AgentRuntimeExecResult
from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR
from awf.node.git_manager import linked_worktree_git_dir, mirror_path_for_worktree


def _isolated_reask_git_metadata_volume_binds(
    worktree_path: Path,
) -> tuple[tempfile.TemporaryDirectory[str] | None, tuple[tuple[Path, str], ...]]:
    """Build credential-free Git discovery binds for a linked re-ask worktree.

    A linked worktree's ``.git`` file points at metadata beneath its shared
    bare mirror. Mounting that whole mirror also exposes its ``config``, which
    can retain HTTPS remote URL userinfo. Git needs only selected linked control
    files plus the existing common ``objects`` and ``refs`` directories to
    recognise the worktree, so mount those directories without exposing the
    mirror configuration.
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
        shutil.copyfile(linked_git_dir / "HEAD", snapshot_path / "HEAD")
        (snapshot_path / "commondir").write_text(
            f"{os.path.relpath(mirror_path, linked_git_dir)}\n", encoding="utf-8"
        )
        (snapshot_path / "gitdir").write_text(f"{DEFAULT_AGENT_WORKDIR}/.git\n", encoding="utf-8")
        source_index = linked_git_dir / "index"
        if source_index.is_file() and not source_index.is_symlink():
            shutil.copyfile(source_index, snapshot_path / "index")
    except OSError:
        if temporary_metadata is not None:
            temporary_metadata.cleanup()
        return None, ()
    return temporary_metadata, (
        (snapshot_path, str(linked_git_dir)),
        (mirror_path / "objects", str(mirror_path / "objects")),
        (mirror_path / "refs", str(mirror_path / "refs")),
    )


def _discard_hosted_execute_task_result(task: asyncio.Task[AgentRuntimeExecResult]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
