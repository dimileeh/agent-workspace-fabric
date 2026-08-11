"""Shared ownership-repair helpers for AWF runtime helpers."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from awf.node.git_manager import (
    git_env_without_object_lookup_overrides,
    linked_worktree_git_dir,
    repair_agent_writable_worktree,
)

AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE = "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"

EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME = (
    "executor.agent_runtime_ownership_repair_failed"
)
MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME = "monitor.agent_runtime_ownership_repair_failed"

_PINNED_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_GIT_METADATA_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_MAX_SOURCE_WORKTREE_GIT_METADATA_BYTES: Final = 1024 * 1024
_PINNED_SOURCE_HEAD_RESOLUTION_TIMEOUT_SECONDS: Final = 30.0


@dataclass(frozen=True)
class ValidatedSourceWorktreeGitContext:
    """Validated linked-worktree Git metadata pinned to an open directory and commit."""

    mirror_path: Path
    linked_git_dir: Path
    linked_git_dir_fd: int
    head_snapshot: str
    resolved_head: str


def _read_bounded_regular_git_metadata_file_at(
    directory_fd: int,
    filename: str,
    *,
    required: bool = True,
) -> str | None:
    """Read one small Git control file without following a raced replacement."""
    try:
        file_fd = os.open(filename, _GIT_METADATA_FILE_OPEN_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        if not required:
            return None
        raise ValueError(f"refusing ownership repair: missing Git metadata {filename}") from None
    except OSError as exc:
        raise ValueError(f"refusing ownership repair: cannot open Git metadata {filename}") from exc

    try:
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(
                f"refusing ownership repair: Git metadata {filename} must be a regular file"
            )
        if file_stat.st_size > _MAX_SOURCE_WORKTREE_GIT_METADATA_BYTES:
            raise ValueError(
                f"refusing ownership repair: Git metadata {filename} exceeds size limit"
            )
        chunks: list[bytes] = []
        total_bytes = 0
        while total_bytes < _MAX_SOURCE_WORKTREE_GIT_METADATA_BYTES:
            chunk = os.read(
                file_fd,
                min(64 * 1024, _MAX_SOURCE_WORKTREE_GIT_METADATA_BYTES - total_bytes),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total_bytes += len(chunk)
        if total_bytes == _MAX_SOURCE_WORKTREE_GIT_METADATA_BYTES and os.read(file_fd, 1):
            raise ValueError(
                f"refusing ownership repair: Git metadata {filename} exceeds size limit"
            )
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"refusing ownership repair: Git metadata {filename} is not valid UTF-8"
            ) from exc
    except OSError as exc:
        raise ValueError(f"refusing ownership repair: cannot read Git metadata {filename}") from exc
    finally:
        os.close(file_fd)


def _source_head_snapshot_ref(head_snapshot: str) -> str | None:
    """Return one safe commit-ish from a regular-file ``HEAD`` snapshot."""
    snapshot_ref = head_snapshot.strip()
    if snapshot_ref.startswith("ref: "):
        symbolic_ref = snapshot_ref.removeprefix("ref: ").strip()
        return symbolic_ref if symbolic_ref.startswith("refs/") else None
    if len(snapshot_ref) not in {40, 64}:
        return None
    if not all(char in "0123456789abcdefABCDEF" for char in snapshot_ref):
        return None
    return snapshot_ref


def _snapshot_pinned_source_symbolic_ref(
    source_mirror: Path,
    symbolic_ref: str,
) -> str:
    """Return one commit ID from a symbolic HEAD ref in a pinned mirror."""
    try:
        result = subprocess.run(
            [
                "git",
                "--git-dir",
                str(source_mirror),
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "--count=2",
                symbolic_ref,
            ],
            capture_output=True,
            text=True,
            timeout=_PINNED_SOURCE_HEAD_RESOLUTION_TIMEOUT_SECONDS,
            env=git_env_without_object_lookup_overrides(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("refusing ownership repair: cannot resolve source Git HEAD") from exc

    refs = result.stdout.strip().splitlines()
    if result.returncode != 0 or len(refs) != 1:
        raise ValueError("refusing ownership repair: cannot resolve source Git HEAD")
    ref_name, separator, snapshot_commit = refs[0].partition(" ")
    if (
        ref_name != symbolic_ref
        or not separator
        or _source_head_snapshot_ref(snapshot_commit) != snapshot_commit
    ):
        raise ValueError("refusing ownership repair: cannot resolve source Git HEAD")
    return snapshot_commit


def _resolve_pinned_source_head(
    source_mirror: Path,
    *,
    linked_git_dir_fd: int,
) -> tuple[str, str]:
    """Capture a stable source ``HEAD``/ref pair and resolve its commit ID."""
    source_mirror_fd: int | None = None
    try:
        source_mirror_fd = os.open(source_mirror, _PINNED_DIRECTORY_OPEN_FLAGS)
        pinned_source_mirror = Path(f"/proc/{os.getpid()}/fd/{source_mirror_fd}")
        initial_head_snapshot = _read_bounded_regular_git_metadata_file_at(
            linked_git_dir_fd, "HEAD"
        )
        assert initial_head_snapshot is not None
        snapshot_ref = _source_head_snapshot_ref(initial_head_snapshot)
        if snapshot_ref is None:
            raise ValueError("refusing ownership repair: invalid source Git HEAD snapshot")
        snapshot_commit = (
            _snapshot_pinned_source_symbolic_ref(pinned_source_mirror, snapshot_ref)
            if snapshot_ref.startswith("refs/")
            else snapshot_ref
        )
        head_snapshot = _read_bounded_regular_git_metadata_file_at(linked_git_dir_fd, "HEAD")
        assert head_snapshot is not None
        if head_snapshot != initial_head_snapshot:
            raise ValueError("refusing ownership repair: source Git HEAD changed while resolving")
        result = subprocess.run(
            [
                "git",
                "--git-dir",
                str(pinned_source_mirror),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{snapshot_commit}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            timeout=_PINNED_SOURCE_HEAD_RESOLUTION_TIMEOUT_SECONDS,
            env=git_env_without_object_lookup_overrides(),
        )
        resolved_head = result.stdout.strip()
        if (
            result.returncode != 0
            or _source_head_snapshot_ref(resolved_head) != resolved_head
            or resolved_head.lower() != snapshot_commit.lower()
        ):
            raise ValueError("refusing ownership repair: cannot resolve source Git HEAD")
        if snapshot_ref.startswith("refs/") and (
            _snapshot_pinned_source_symbolic_ref(pinned_source_mirror, snapshot_ref)
            != snapshot_commit
        ):
            raise ValueError("refusing ownership repair: source Git HEAD changed while resolving")
        return head_snapshot, resolved_head
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("refusing ownership repair: cannot resolve source Git HEAD") from exc
    finally:
        if source_mirror_fd is not None:
            os.close(source_mirror_fd)


def _linked_worktree_git_dir_from_contents(worktree_path: Path, content: str) -> Path:
    """Resolve the Git admin directory named by a validated source `.git` file."""
    prefix = "gitdir: "
    if not content.startswith(prefix):
        raise ValueError(
            "refusing ownership repair: source workspace Git metadata lacks a gitdir pointer"
        )
    git_dir = Path(content.removeprefix(prefix).strip())
    if not git_dir.is_absolute():
        git_dir = worktree_path / git_dir
    try:
        return git_dir.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            "refusing ownership repair: cannot resolve source workspace Git metadata "
            f"for workspace {worktree_path}"
        ) from exc


def _mirror_path_from_linked_git_dir(
    linked_git_dir: Path, *, linked_git_dir_fd: int | None = None
) -> Path:
    """Resolve a linked worktree's mirror from an already trusted gitdir read."""
    commondir = linked_git_dir / "commondir"
    try:
        if linked_git_dir_fd is not None:
            raw_common_dir = _read_bounded_regular_git_metadata_file_at(
                linked_git_dir_fd, "commondir", required=False
            )
            if raw_common_dir is not None:
                raw_common_dir = raw_common_dir.strip()
        elif commondir.is_file():
            raw_common_dir = commondir.read_text(encoding="utf-8").strip()
        else:
            raw_common_dir = None
        if raw_common_dir:
            common_dir = Path(raw_common_dir)
            if not common_dir.is_absolute():
                common_dir = linked_git_dir / common_dir
            return common_dir.resolve()
        if str(linked_git_dir).startswith(f"/proc/{os.getpid()}/fd/"):
            return (linked_git_dir / ".." / "..").resolve()
        return linked_git_dir.parent.parent.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "refusing ownership repair: cannot resolve mirror path from linked-worktree "
            f"git metadata {linked_git_dir}"
        ) from exc


def _validate_linked_git_dir_backref(
    linked_git_dir: Path,
    worktree_path: Path,
    *,
    linked_git_dir_fd: int | None = None,
) -> None:
    """Validate Git's reciprocal metadata pointer for suffixed worktree dirs."""
    metadata_gitdir = linked_git_dir / "gitdir"
    expected_git_file = worktree_path / ".git"
    if linked_git_dir_fd is None and (
        expected_git_file.is_symlink() or not expected_git_file.is_file()
    ):
        raise ValueError(
            "refusing ownership repair: workspace git metadata must be a "
            f"non-symlink file at {expected_git_file}"
        )
    try:
        if linked_git_dir_fd is not None:
            raw_gitdir = _read_bounded_regular_git_metadata_file_at(linked_git_dir_fd, "gitdir")
            assert raw_gitdir is not None
            raw_gitdir = raw_gitdir.strip()
        else:
            raw_gitdir = metadata_gitdir.read_text(encoding="utf-8").strip()
        if not raw_gitdir:
            raise ValueError(
                "refusing ownership repair: linked-worktree metadata has an empty "
                f"gitdir back-reference at {metadata_gitdir}"
            )
        git_file = Path(raw_gitdir)
        if not git_file.is_absolute():
            git_file = linked_git_dir / git_file
        resolved_git_file = git_file.resolve()
        resolved_expected_git_file = expected_git_file.resolve()
    except OSError as exc:
        raise ValueError(
            "refusing ownership repair: cannot read linked-worktree metadata "
            f"gitdir back-reference at {metadata_gitdir}"
        ) from exc
    except RuntimeError as exc:
        raise ValueError(
            "refusing ownership repair: cannot resolve linked-worktree metadata "
            f"gitdir back-reference at {metadata_gitdir}"
        ) from exc

    if resolved_git_file != resolved_expected_git_file:
        raise ValueError(
            "refusing ownership repair: linked-worktree metadata points to another "
            f"workspace. expected gitdir {resolved_expected_git_file}, got {resolved_git_file}"
        )


def _validated_layout_mirror_for_linked_git_dir(
    linked_git_dir: Path,
    *,
    linked_git_dir_name: str,
    worktree_path: Path,
    workspace_id: str,
    linked_git_dir_fd: int | None = None,
) -> Path:
    """Validate a linked-worktree directory and return its trusted mirror.

    Control-plane control over git pointers has been compromised during
    monitor recoveries; trust only mirrored worktree pointers that stay under
    the expected ``<worktrees_root>/../mirrors`` hierarchy for this
    worktree path and match this workspace's metadata entry.
    """
    if linked_git_dir_fd is None:
        mirror_path = _mirror_path_from_linked_git_dir(linked_git_dir)
    else:
        mirror_path = _mirror_path_from_linked_git_dir(
            linked_git_dir, linked_git_dir_fd=linked_git_dir_fd
        )
    expected_mirror_root = worktree_path.parent.parent / "mirrors"
    resolved_expected_root = expected_mirror_root.resolve()
    resolved_mirror = mirror_path.resolve()
    if not resolved_mirror.is_relative_to(resolved_expected_root):
        raise ValueError(
            "refusing ownership repair: mirror path is outside expected mirrors root "
            f"for workspace {worktree_path}: {resolved_mirror}"
        )

    expected_worktree_git_root = (resolved_mirror / "worktrees").resolve()
    if (linked_git_dir / "..").resolve() != expected_worktree_git_root:
        raise ValueError(
            "refusing ownership repair: linked-worktree metadata points to another "
            f"workspace. expected parent {expected_worktree_git_root}, got {linked_git_dir.parent}"
        )

    if linked_git_dir_name != workspace_id:
        linked_git_dir_suffix = linked_git_dir_name.removeprefix(workspace_id)
        if not linked_git_dir_name.startswith(workspace_id) or not linked_git_dir_suffix.isdigit():
            raise ValueError(
                "refusing ownership repair: linked-worktree metadata points to another "
                f"workspace. expected workspace id {workspace_id}, got {linked_git_dir_name}"
            )
        if linked_git_dir_fd is None:
            _validate_linked_git_dir_backref(linked_git_dir, worktree_path)
        else:
            _validate_linked_git_dir_backref(
                linked_git_dir, worktree_path, linked_git_dir_fd=linked_git_dir_fd
            )

    return mirror_path


def _validated_layout_mirror_for_worktree(
    worktree_path: Path, workspace_id: str
) -> tuple[Path, Path]:
    """Resolve and validate the linked-worktree mirror and gitdir.

    Control-plane control over git pointers has been compromised during
    monitor recoveries; trust only mirrored worktree pointers that stay under
    the expected ``<worktrees_root>/../mirrors`` hierarchy for this
    worktree path and match this workspace's metadata entry.
    """
    linked_git_dir = linked_worktree_git_dir(worktree_path)
    if linked_git_dir is None:
        raise ValueError(
            "refusing ownership repair: cannot read linked-worktree git metadata "
            f"for workspace {worktree_path}"
        )
    mirror_path = _validated_layout_mirror_for_linked_git_dir(
        linked_git_dir,
        linked_git_dir_name=linked_git_dir.name,
        worktree_path=worktree_path,
        workspace_id=workspace_id,
    )
    return mirror_path, linked_git_dir


def validated_source_worktree_git_context(
    worktree_path: Path, workspace_id: str
) -> ValidatedSourceWorktreeGitContext:
    """Return trusted source Git metadata pinned through HEAD resolution.

    Unlike ownership repair for an existing primary checkout, a caller about
    to create another worktree must verify the reciprocal ``gitdir`` entry
    even when Git assigned the workspace's exact identifier. Otherwise a
    writable primary `.git` file can be redirected to another repository's
    linked-worktree metadata before the caller resolves HEAD. The returned
    procfs directory path names the opened descriptor, so replacing the
    writable admin-directory path after validation cannot redirect Git.
    """
    try:
        worktree_fd = os.open(worktree_path, _PINNED_DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise ValueError(
            "refusing ownership repair: cannot open source workspace Git metadata "
            f"for workspace {worktree_path}"
        ) from exc
    try:
        source_git_file = _read_bounded_regular_git_metadata_file_at(worktree_fd, ".git")
        assert source_git_file is not None
        linked_git_dir = _linked_worktree_git_dir_from_contents(worktree_path, source_git_file)
    finally:
        os.close(worktree_fd)
    try:
        linked_git_dir_fd = os.open(linked_git_dir, _PINNED_DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise ValueError(
            "refusing ownership repair: cannot open linked-worktree git metadata "
            f"for workspace {worktree_path}"
        ) from exc

    pinned_linked_git_dir = Path(f"/proc/{os.getpid()}/fd/{linked_git_dir_fd}")
    try:
        mirror_path = _validated_layout_mirror_for_linked_git_dir(
            pinned_linked_git_dir,
            linked_git_dir_name=linked_git_dir.name,
            worktree_path=worktree_path,
            workspace_id=workspace_id,
            linked_git_dir_fd=linked_git_dir_fd,
        )
        _validate_linked_git_dir_backref(
            pinned_linked_git_dir,
            worktree_path,
            linked_git_dir_fd=linked_git_dir_fd,
        )
        head_snapshot, resolved_head = _resolve_pinned_source_head(
            mirror_path,
            linked_git_dir_fd=linked_git_dir_fd,
        )
    except BaseException:
        os.close(linked_git_dir_fd)
        raise
    return ValidatedSourceWorktreeGitContext(
        mirror_path=mirror_path,
        linked_git_dir=pinned_linked_git_dir,
        linked_git_dir_fd=linked_git_dir_fd,
        head_snapshot=head_snapshot,
        resolved_head=resolved_head,
    )


def _repair_agent_runtime_ownership_in_thread(
    worktree_path: Path,
    workspace_id: str,
    linked_worktree_id: str | None,
    repair_shared_git_metadata: bool = True,
) -> None:
    """Repair ownership using a linked-worktree identifier when one is supplied."""
    if linked_worktree_id is not None and linked_worktree_id != worktree_path.name:
        raise ValueError(
            "refusing ownership repair: temporary linked-worktree identifier does not "
            f"match worktree path {worktree_path}"
        )
    layout_mirror, validated_linked_git_dir = _validated_layout_mirror_for_worktree(
        worktree_path,
        linked_worktree_id or workspace_id,
    )
    if repair_shared_git_metadata:
        repair_agent_writable_worktree(
            layout_mirror,
            worktree_path,
            linked_git_dir=validated_linked_git_dir,
        )
    else:
        repair_agent_writable_worktree(
            layout_mirror,
            worktree_path,
            linked_git_dir=validated_linked_git_dir,
            repair_shared_git_metadata=False,
        )


class _LoggerProtocol(Protocol):
    """Protocol contract for ownership-repair logging callsites."""

    def exception(
        self,
        event: str,
        *,
        workspace_id: str,
        worktree_path: str,
        reason: str,
        reason_code: str,
    ) -> None:
        """Emit a structured exception event for ownership-repair failures."""
        ...


async def repair_agent_runtime_ownership(
    *,
    logger: _LoggerProtocol,
    workspace_id: str,
    worktree_path: Path,
    reason: str,
    event_name: str,
    reason_code: str = AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    linked_worktree_id: str | None = None,
    repair_shared_git_metadata: bool = True,
) -> bool:
    """Attempt to repair runtime ownership for an agent worktree.

    ``linked_worktree_id`` is the Git metadata name for a temporary linked
    worktree whose name differs from the owning workspace identifier.
    """
    if os.geteuid() != 0:
        return True
    try:
        if repair_shared_git_metadata:
            await asyncio.to_thread(
                _repair_agent_runtime_ownership_in_thread,
                worktree_path,
                workspace_id,
                linked_worktree_id,
            )
        else:
            await asyncio.to_thread(
                _repair_agent_runtime_ownership_in_thread,
                worktree_path,
                workspace_id,
                linked_worktree_id,
                repair_shared_git_metadata,
            )
    except Exception:
        logger.exception(
            event_name,
            workspace_id=workspace_id,
            worktree_path=str(worktree_path),
            reason=reason,
            reason_code=reason_code,
        )
        return False
    return True
