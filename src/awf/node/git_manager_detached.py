"""Detached worktree materialization for trusted-base profile snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from awf.node.git_manager import GitManager, WorktreeLayout


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

    Raises ``GitOperationError`` with:
    - ``GIT_WORKTREE_ALREADY_EXISTS`` when the path is already present
    - ``GIT_BASE_BRANCH_MISSING`` when the commit cannot be resolved in the mirror
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

    lock = manager._lock_for_mirror(mirror_path)
    async with lock:
        try:
            await manager._run(
                [
                    "git",
                    "--git-dir",
                    str(mirror_path),
                    "rev-parse",
                    "--verify",
                    f"{cleaned_sha}^{{commit}}",
                ],
                operation="mirror.rev-parse_commit",
            )
        except GitOperationError:
            # Commit may exist only on a recently updated remote tip that
            # ``ensure_mirror`` has not yet advertised as a peelable object
            # under some shallow/partial mirrors — try a targeted fetch.
            try:
                await manager._run(
                    [
                        "git",
                        "--git-dir",
                        str(mirror_path),
                        "fetch",
                        "--no-tags",
                        "origin",
                        cleaned_sha,
                    ],
                    operation="mirror.fetch_commit",
                )
                await manager._run(
                    [
                        "git",
                        "--git-dir",
                        str(mirror_path),
                        "rev-parse",
                        "--verify",
                        f"{cleaned_sha}^{{commit}}",
                    ],
                    operation="mirror.rev-parse_commit",
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
            [
                "git",
                "--git-dir",
                str(mirror_path),
                "worktree",
                "add",
                "--detach",
                str(worktree_path),
                cleaned_sha,
            ],
            operation="worktree.add_detached",
        )

    # Ephemeral profile snapshot — no agent runtime will write here.
    return WorktreeLayout(
        mirror_path=mirror_path,
        worktree_path=worktree_path,
        branch_name="",
    )
