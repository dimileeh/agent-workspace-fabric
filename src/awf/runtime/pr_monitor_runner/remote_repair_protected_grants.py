"""Operator-grant and preserved-commit checks for protected-scope repair.

Split out of ``awf.runtime.pr_monitor_runner.remote_repair_protected`` to keep
modules under the first-party file-size guardrail; behavior is unchanged. These
helpers decide whether an approve-and-keep grant authorizes publishing a
preserved protected commit, and whether that commit is already on (or would be
published to) the remote PR branch.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from awf.control.operator_grants import (
    active_operator_grant_specs_in_session,
    consume_active_operator_grants_in_session,
)
from awf.control.quality_gates import (
    GrantSpec,
    _grant_matches,
)
from awf.db.repositories import (
    WorkspaceRepository,
)
from awf.runtime.pr_monitor_runner.git_utils import (
    git_worktree_command,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
)


async def _active_operator_grant_specs(self: Any, workspace_id: str) -> list[GrantSpec]:
    """Return the operator grants active for the workspace's CURRENT block epoch.

    Delegates to the shared session-bound loader so the monitor's grant-aware
    protected-scope push checks use exactly the same query as the executor's
    pre-PR resume path (no second grant model). Empty for a workspace that is not
    a resumed approve-and-keep block, so threading it into every gate caller is a
    no-op outside the resume path."""
    async with self._deps.session_factory() as session:
        return await active_operator_grant_specs_in_session(session, workspace_id)


async def _preserved_protected_change_fully_granted(
    self: Any,
    *,
    workspace_id: str,
    grant_specs: Sequence[GrantSpec],
) -> bool:
    """Return whether active grants cover every protected path of the current block.

    A combined ``--directive ... --grant ...`` resume can intentionally KEEP the
    preserved protected edit while the directive fixes other files: the same-request
    grant (preserved across the combined guide) suppresses the net-diff violation, so
    ``_protected_scope_push_block`` returns None even though the preserved commit is
    still in the unpushed range. The directive leak guard must NOT treat that
    operator-approved keep as an ungranted leak. When every protected path recorded
    for the workspace's current block (``block_violations``, i.e. exactly the
    protected paths the preserved commit changed) is covered by an active grant,
    publishing the preserved commit is the operator's authorized decision
    (PRRT_kwDOSJAM6s6KG1hs). A grant that covers only SOME of those paths — or none —
    still leaves an ungranted protected change, so the guard must keep firing."""
    if not grant_specs:
        return False
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            return False
        violation_paths = [
            str(violation.get("path"))
            for violation in (workspace.block_violations or [])
            if isinstance(violation, dict) and violation.get("path")
        ]
    if not violation_paths:
        return False
    return all(
        any(_grant_matches(path, grant.path) for grant in grant_specs) for path in violation_paths
    )


async def _preserved_commit_already_on_remote(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    remote_push_url: str | None = None,
    preserved_head_sha: str | None = None,
) -> bool:
    """Return whether the preserved commit is already on the remote PR branch.

    Divergence recovery (WS-2 §5): a monitor/worker restart can re-run the
    post-PR resume after the preserved commit's push already landed. Pushing
    again would either no-op noisily or surface a spurious non-fast-forward, so
    compare the worktree HEAD against the freshly-fetched remote branch: an empty
    changed-path set means HEAD is an ancestor of (or equals) the remote head —
    already pushed — so the caller treats the push as a no-op. A fetch/diff
    failure returns ``False`` so the normal push path runs and surfaces the
    error rather than silently skipping a real push.

    An empty changed-path set only proves the CURRENT worktree HEAD content is on
    the remote. If the worktree was reset or recreated at the remote PR head
    during the blocked interval, HEAD no longer carries the operator-approved
    preserved commit yet the diff is still empty — declaring a no-op would
    silently drop the approved protected commit. When the pause recorded a
    preserved HEAD SHA (``preserved_head_sha``), positively confirm THAT commit
    is contained in the freshly-fetched remote head before declaring the no-op;
    if it is not, return ``False`` so the caller does not skip a real push."""
    if not worktree_path.exists():
        return False
    try:
        _local_base, changed_paths = await self._remote_branch_diff_base_and_changed_paths(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
        )
    except ProtectedScopeDiffError:
        return False
    if changed_paths:
        return False
    if preserved_head_sha and not await self._preserved_head_on_remote_fetch_head(
        worktree_path=worktree_path,
        preserved_head_sha=preserved_head_sha,
    ):
        _log.warning(
            "monitor.protected_scope_preserved_commit_not_on_remote",
            workspace_id=workspace_id,
            remote_branch=remote_branch,
            preserved_head_sha=preserved_head_sha,
        )
        return False
    _log.info(
        "monitor.protected_scope_preserved_commit_already_on_remote",
        workspace_id=workspace_id,
        remote_branch=remote_branch,
    )
    return True


async def _preserved_head_on_remote_fetch_head(
    self: Any,
    *,
    worktree_path: Path,
    preserved_head_sha: str,
) -> bool:
    """Return whether ``preserved_head_sha`` is contained in the fetched remote.

    Runs ``git merge-base --is-ancestor`` against ``FETCH_HEAD`` (freshly written
    by :meth:`_remote_branch_diff_base_and_changed_paths`), which exits 0 when the
    preserved commit is an ancestor of — or equal to — the remote PR head. A
    non-zero exit (including an unknown SHA) means the preserved commit never
    landed, so the caller must not treat the push as a no-op."""
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "merge-base",
            "--is-ancestor",
            preserved_head_sha,
            "FETCH_HEAD",
        )
    )
    return bool(result.ok)


async def _preserved_commit_in_unpushed_range(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    remote_push_url: str | None = None,
    preserved_head_sha: str | None = None,
) -> bool:
    """Return whether a DIRECTIVE push would still publish the preserved commit.

    A DIRECTIVE (revert/redo) resume must NOT publish the ungranted protected
    commit — only an approve-and-keep grant authorizes that. If the directive
    resolves the block by adding a revert commit ON TOP of the preserved offending
    commit (instead of resetting it away), the final tree can match the remote PR
    branch, so the net-diff ``_protected_scope_push_block`` finds no violation
    (PRRT_kwDOSJAM6s6KFytV scoped the no-op guard to grant-only resumes). Pushing
    HEAD would still fast-forward the remote branch over the original ungranted
    protected-file commit plus its revert.

    Detect that by confirming the preserved commit is (a) an ancestor of the local
    HEAD this push would publish AND (b) not already contained in the
    freshly-fetched remote PR head (an already-published commit cannot be
    un-leaked, and a grant-only resume legitimately lands it). When the preserved
    commit is NOT an ancestor of HEAD the directive reset it away — no leak — so
    return ``False``. But once (a) is confirmed the push WOULD publish the commit,
    so a failed containment fetch cannot prove it is already on the remote: fail
    closed (return ``True``) to surface needs_human rather than leak the ungranted
    protected commit, since the normal push may still succeed despite this fetch
    failing."""
    if not preserved_head_sha or not worktree_path.exists():
        return False
    ancestry = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "merge-base",
            "--is-ancestor",
            preserved_head_sha,
            "HEAD",
        )
    )
    if not ancestry.ok:
        # The directive reset the preserved commit away (it is no longer reachable
        # from HEAD), so this push cannot publish it: the legitimate revert path.
        return False
    try:
        # Populate FETCH_HEAD with the remote PR head for the containment check.
        await self._remote_branch_diff_base_and_changed_paths(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
        )
    except ProtectedScopeDiffError:
        # Ancestry is already confirmed above, so this push WOULD publish the
        # preserved commit. A failed containment fetch cannot prove the commit is
        # already on the remote, so fail closed: block the leak and surface
        # needs_human rather than let the (still potentially-succeeding) push leak
        # the ungranted protected commit.
        _log.warning(
            "monitor.protected_scope_directive_preserved_commit_fetch_failed",
            workspace_id=workspace_id,
            remote_branch=remote_branch,
            preserved_head_sha=preserved_head_sha,
        )
        return True
    if await self._preserved_head_on_remote_fetch_head(
        worktree_path=worktree_path,
        preserved_head_sha=preserved_head_sha,
    ):
        return False
    _log.warning(
        "monitor.protected_scope_directive_preserved_commit_in_push_range",
        workspace_id=workspace_id,
        remote_branch=remote_branch,
        preserved_head_sha=preserved_head_sha,
    )
    return True


async def _consume_active_operator_grants(self: Any, workspace_id: str) -> int:
    """Mark the workspace's active operator grants consumed (single-use).

    Called once a post-PR resume clears the protected gate and pushes, so a later
    DIFFERENT protected change re-blocks and must be granted again. Delegates to
    the shared session-bound consumer (same as the executor pre-PR resume)."""
    async with self._deps.session_factory() as session:
        consumed = await consume_active_operator_grants_in_session(session, workspace_id)
        if consumed:
            await session.commit()
        return consumed
