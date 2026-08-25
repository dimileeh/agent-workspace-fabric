"""Detached worktree materialization for trusted-base profile snapshots."""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.profile_paths import PROFILE_MARKER_PATHS
from awf.node.git_manager_ownership import (
    TRUSTED_BASE_GIT_CONFIG_ARGS,
    git_env_for_trusted_base_materialization,
)

if TYPE_CHECKING:
    from awf.node.git_manager import GitManager, GitResult, WorktreeLayout

_TRUSTED_PROFILE_MISMATCH_REASON = "GIT_TRUSTED_BASE_PROFILE_MISMATCH"


def _trusted_git_args(mirror_path: Path, *tail: str) -> list[str]:
    """Build a git argv that disables checkout attributes/hooks for the mirror."""
    return [
        "git",
        *TRUSTED_BASE_GIT_CONFIG_ARGS,
        "--git-dir",
        str(mirror_path),
        *tail,
    ]


async def add_detached_worktree_at_commit(
    manager: GitManager,
    *,
    workspace_id: str,
    repo_url: str,
    commit_sha: str,
) -> WorktreeLayout:
    """Materialize a detached read-only worktree at an immutable commit SHA.

    Used for trusted-base profile resolution during adopted ``sync_feature_pr``
    provisioning: the durable workspace worktree remains the PR head, while
    this ephemeral snapshot exposes the adopted target-base tree. The caller
    must always reclaim via ``remove_worktree`` (success and failure).

    Rev-parse, fetch, and worktree checkout run with replace refs, grafts,
    object/config overrides, and checkout attributes/hooks disabled. Profile
    marker files are then verified (and rewritten) against the raw commit blob
    so filter/replace poison on a shared mirror cannot authorize attacker
    profile bytes under an unchanged SHA.

    Raises ``GitOperationError`` with:
    - ``GIT_WORKTREE_ALREADY_EXISTS`` when the path is already present
    - ``GIT_BASE_BRANCH_MISSING`` when the commit cannot be resolved in the mirror
    - ``GIT_TRUSTED_BASE_PROFILE_MISMATCH`` when a profile marker appears on disk
      (including as a leaf symlink) without a matching blob in the raw commit
    """
    # Late import: ``git_manager`` loads this module while defining ``GitManager``.
    from awf.node.git_manager import GitOperationError, WorktreeLayout

    cleaned_sha = (commit_sha or "").strip()
    if not (
        len(cleaned_sha) == 40 and all(char in "0123456789abcdefABCDEF" for char in cleaned_sha)
    ):
        raise GitOperationError(
            operation="worktree.add_detached",
            returncode=1,
            stdout="",
            stderr=(
                "exact immutable full commit SHA (40 hex) is required for "
                "detached worktree materialization"
            ),
            reason_code="GIT_BASE_BRANCH_MISSING",
        )

    worktree_path = manager._worktree_path_for(workspace_id)
    mirror_path = await manager.ensure_mirror(repo_url)
    manager._worktrees_dir.mkdir(parents=True, exist_ok=True)

    if worktree_path.exists():
        raise GitOperationError(
            operation="worktree.add_detached",
            returncode=1,
            stdout="",
            stderr=f"worktree path already exists: {worktree_path}",
            reason_code="GIT_WORKTREE_ALREADY_EXISTS",
        )

    trusted_env = git_env_for_trusted_base_materialization(manager._effective_env())

    lock = manager._lock_for_mirror(mirror_path)
    async with lock:
        try:
            await manager._run(
                _trusted_git_args(
                    mirror_path,
                    "rev-parse",
                    "--verify",
                    f"{cleaned_sha}^{{commit}}",
                ),
                operation="mirror.rev-parse_commit",
                env=trusted_env,
            )
        except GitOperationError:
            # Commit may exist only on a recently updated remote tip that
            # ``ensure_mirror`` has not yet advertised as a peelable object
            # under some shallow/partial mirrors — try a targeted fetch.
            try:
                await manager._run(
                    _trusted_git_args(
                        mirror_path,
                        "fetch",
                        "--no-tags",
                        "origin",
                        cleaned_sha,
                    ),
                    operation="mirror.fetch_commit",
                    env=trusted_env,
                )
                await manager._run(
                    _trusted_git_args(
                        mirror_path,
                        "rev-parse",
                        "--verify",
                        f"{cleaned_sha}^{{commit}}",
                    ),
                    operation="mirror.rev-parse_commit",
                    env=trusted_env,
                )
            except GitOperationError as exc:
                raise GitOperationError(
                    operation="worktree.add_detached",
                    returncode=exc.returncode,
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                    reason_code="GIT_BASE_BRANCH_MISSING",
                ) from exc

        await manager._run(
            _trusted_git_args(
                mirror_path,
                "worktree",
                "add",
                "--detach",
                str(worktree_path),
                cleaned_sha,
            ),
            operation="worktree.add_detached",
            env=trusted_env,
        )
        try:
            await _verify_and_materialize_trusted_profile_markers(
                manager,
                mirror_path=mirror_path,
                worktree_path=worktree_path,
                commit_sha=cleaned_sha,
                env=trusted_env,
            )
        except BaseException:
            # Leave no half-trusted snapshot path behind on verify failure.
            with contextlib.suppress(GitOperationError):
                await manager._run(
                    _trusted_git_args(
                        mirror_path,
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree_path),
                    ),
                    operation="worktree.remove_trusted_base_verify_failed",
                    env=trusted_env,
                )
            if worktree_path.exists():
                # Best-effort under the mirror lock: avoid stranding a dir that
                # would trip GIT_WORKTREE_ALREADY_EXISTS on retry.
                shutil.rmtree(worktree_path, ignore_errors=True)
            raise

    # Ephemeral profile snapshot — no agent runtime will write here.
    return WorktreeLayout(
        mirror_path=mirror_path,
        worktree_path=worktree_path,
        branch_name="",
    )


async def _verify_and_materialize_trusted_profile_markers(
    manager: GitManager,
    *,
    mirror_path: Path,
    worktree_path: Path,
    commit_sha: str,
    env: dict[str, str],
) -> None:
    """Ensure profile markers match the raw commit blob (no smudge / replace).

    ``git show <sha>:<path>`` returns object-store bytes without checkout filters.
    Disk files that exist without a blob fail closed. When the blob exists, the
    worktree file is rewritten to those raw bytes so filter poison cannot reach
    profile resolve under an unchanged commit SHA.

    Leaf symlinks from checkout are unlinked before the rewrite: ``Path.write_bytes``
    follows links, which would corrupt a relative target or overwrite an absolute
    host path under the provisioner's privileges.
    """
    from awf.node.git_manager import GitOperationError

    for relative in PROFILE_MARKER_PATHS:
        disk_path = worktree_path / relative
        raw = await _raw_commit_blob_bytes(
            manager,
            mirror_path=mirror_path,
            commit_sha=commit_sha,
            relative_path=relative,
            env=env,
        )
        # ``is_file()`` follows links; include the leaf symlink itself so a dangling
        # Git symlink marker without a blob still fails closed.
        disk_exists = disk_path.is_symlink() or disk_path.is_file()
        if raw is None:
            if disk_exists:
                raise GitOperationError(
                    operation="worktree.verify_trusted_base_profile",
                    returncode=1,
                    stdout="",
                    stderr=(
                        f"trusted-base profile marker {relative!r} present on disk "
                        "but absent from the raw commit; refusing poisoned snapshot"
                    ),
                    reason_code=_TRUSTED_PROFILE_MISMATCH_REASON,
                )
            continue
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        if disk_path.is_symlink():
            disk_path.unlink()
        disk_path.write_bytes(raw)


async def _raw_commit_blob_bytes(
    manager: GitManager,
    *,
    mirror_path: Path,
    commit_sha: str,
    relative_path: str,
    env: dict[str, str],
) -> bytes | None:
    """Return raw blob bytes for ``commit:path``, or None when the path is absent."""
    from awf.node.git_manager import GitOperationError

    try:
        result: GitResult = await manager._run(
            _trusted_git_args(mirror_path, "show", f"{commit_sha}:{relative_path}"),
            operation="mirror.show_trusted_base_profile",
            env=env,
        )
    except GitOperationError as exc:
        combined = f"{exc.stderr or ''}\n{exc.stdout or ''}".lower()
        if (
            "does not exist" in combined
            or "exists on disk" in combined
            or "not in '" in combined
            or "path not in" in combined
            or "fatal: path" in combined
        ):
            return None
        raise
    return result.stdout.encode("utf-8", errors="surrogateescape")
