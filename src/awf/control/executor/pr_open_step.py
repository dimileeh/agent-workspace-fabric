"""WorkspaceExecutor push + open-PR step.

Mechanically extracted from ``execution_flow.execute``; behavior is unchanged
except for execution-time revalidation of a reused PR. Keeping the forge-neutral
``git push`` + PR-open call and its audit/failure handling here keeps
``execution_flow`` focused on the agent-run → commit → validate → push pipeline.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from typing import Any

from awf.common.commands import AsyncCommandRunner
from awf.common.forge import concrete_forge_for_repo, make_forge_client
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_lifecycle import PullRequestLifecycle, PullRequestSnapshot
from awf.common.git_identity import git_safe_directory_config_args
from awf.common.github_client import RepoRef
from awf.common.task_tag import title_with_task_tag
from awf.control.executor.constants import (
    _AUDIT_GIT_PUSH_EVENT,
    _AUDIT_PR_CREATED_EVENT,
    _GIT_PUSH_FAILED_REASON_CODE,
    _PR_CREATE_FAILED_REASON_CODE,
)
from awf.control.executor.helpers import (
    _build_pr_body,
    _existing_pr_remote_push_url,
    _extract_pr_number,
)
from awf.control.executor.quality_gates import _log
from awf.db.enums import FailureReason, TaskKind, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.runtime.pr_creator import PullRequestError
from awf.runtime.pr_push_remote import retained_fork_pr_adoption

_PR_STATE_LOOKUP_FAILED_REASON_CODE = "PR_STATE_LOOKUP_FAILED"
# Post-push forge reads can lag behind a just-completed push. Retry briefly
# before treating an open head that does not yet equal the tip as a race.
_POST_PUSH_TIP_RETRY_ATTEMPTS = 3
_POST_PUSH_TIP_RETRY_DELAY_SECONDS = 0.5


class _PostPushReuseDisposition(StrEnum):
    keep = "keep"
    open_replacement = "open_replacement"
    fail_closed = "fail_closed"


def _apply_sync_feature_replacement_policy(policy: dict[str, Any], *, repo_url: str | None) -> None:
    """Convert closed sync-feature adoption into a coding feature task.

    Clears PR identity inside ``pr_adoption`` but retains distinct fork
    ``head_repo_slug`` / ``head_repo_url`` so replacement pushes stay on the
    fork instead of ``origin``.
    """
    adoption = policy.get("pr_adoption")
    retained = retained_fork_pr_adoption(
        repo_url=repo_url,
        adoption=adoption if isinstance(adoption, dict) else None,
    )
    policy.pop("pr_adoption", None)
    if retained is not None:
        policy["pr_adoption"] = retained
    policy["task_kind"] = TaskKind.feature_branch_pr.value


async def _clear_stale_pr_identity_for_replacement(
    self: Any,
    *,
    workspace_id: str,
    ws: Any,
) -> None:
    """Drop persisted PR identity so push opens a replacement instead of reuse.

    Admission may have copied ``pr_url``/``pr_number``/``remote_push_branch`` from a
    PR that was open at retry time. If that PR has since merged or closed, the
    executor must not hand the monitor a terminal PR URL after pushing new
    commits. Clearing identity (and sync-feature adoption) mirrors the closed-PR
    retry path so the replacement PR is opened on the workspace branch tip.
    Fork head-repo fields are retained so the replacement push still targets the
    adopted fork when that differs from the base repository.
    """
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        persisted = await repo.get(workspace_id)
        if persisted is not None:
            persisted.pr_url = None
            persisted.pr_number = None
            persisted.remote_push_branch = None
            policy = dict(persisted.task_policy or {})
            if persisted.task_kind == TaskKind.sync_feature_pr.value:
                _apply_sync_feature_replacement_policy(
                    policy,
                    repo_url=getattr(persisted, "repo_url", None) or getattr(ws, "repo_url", None),
                )
                persisted.task_kind = TaskKind.feature_branch_pr.value
                persisted.task_policy = policy
            await session.commit()
    ws.pr_url = None
    ws.pr_number = None
    ws.remote_push_branch = None
    if getattr(ws, "task_kind", None) == TaskKind.sync_feature_pr.value:
        ws.task_kind = TaskKind.feature_branch_pr.value
        policy = dict(ws.task_policy or {})
        _apply_sync_feature_replacement_policy(
            policy,
            repo_url=getattr(ws, "repo_url", None),
        )
        ws.task_policy = policy


async def _sync_reuse_remote_push_branch(
    self: Any,
    *,
    workspace_id: str,
    ws: Any,
    live_head_ref: str,
) -> None:
    """Persist the live PR head so reuse pushes to the branch the forge tracks.

    Admission may have copied ``remote_push_branch`` while the PR was open under
    an older head name. If the head was renamed before this push, reusing the
    stale name would update a detached branch while monitoring continues on the
    renamed PR head — and could merge without the retry tip.
    """
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        persisted = await repo.get(workspace_id)
        if persisted is not None:
            persisted.remote_push_branch = live_head_ref
            await session.commit()
    ws.remote_push_branch = live_head_ref


async def _live_head_descends_from_pushed(
    *,
    runner: AsyncCommandRunner,
    worktree_path: Path,
    ancestor_sha: str,
    descendant_sha: str,
    fetch_remote: str = "origin",
) -> bool:
    """Return True when ``descendant_sha`` is a descendant of ``ancestor_sha``.

    Used after a reuse push when the live PR head moved forward (ordinary
    fast-forward) so containment is not exact SHA equality. Fetches the live
    tip when it is not yet in the worktree. A force-push that dropped the
    pushed tip fails this check.
    """
    git_prefix = [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
    ]
    has_object = await runner.run(
        [*git_prefix, "cat-file", "-e", f"{descendant_sha}^{{commit}}"],
    )
    if not has_object.ok:
        fetched = await runner.run(
            [*git_prefix, "fetch", "--no-tags", fetch_remote, descendant_sha],
        )
        if not fetched.ok:
            return False
    ancestor = await runner.run(
        [*git_prefix, "merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
    )
    return bool(ancestor.ok)


async def _pr_snapshot_contains_pushed_tip(
    *,
    snapshot: PullRequestSnapshot,
    pushed_head_sha: str | None,
    runner: AsyncCommandRunner | None = None,
    worktree_path: Path | None = None,
    fetch_remote: str = "origin",
) -> bool:
    """Return whether the forge snapshot's head still contains the tip we pushed.

    Compares forge ``head_sha`` (not ``merge_commit`` ancestry): squash merges
    create a new merge OID that does not contain the PR tip as a git ancestor,
    but ``headRefOid`` / source commit still names the tip that was merged.

    Exact OID equality is the common case. When another actor fast-forward-
    pushes a descendant between our non-force push and this snapshot, the live
    head still contains the validated tip — verify descent via
    ``merge-base --is-ancestor``. A concurrent force-push that rewrote the head
    off the tip fails both checks.
    """
    if not pushed_head_sha:
        return False
    live_head_sha = snapshot.head_sha
    if not isinstance(live_head_sha, str) or not live_head_sha:
        return False
    if pushed_head_sha.lower() == live_head_sha.lower():
        return True
    if runner is None or worktree_path is None:
        return False
    return await _live_head_descends_from_pushed(
        runner=runner,
        worktree_path=worktree_path,
        ancestor_sha=pushed_head_sha,
        descendant_sha=live_head_sha,
        fetch_remote=fetch_remote,
    )


async def _resolve_post_push_reuse(
    *,
    forge_client: Any,
    repo: RepoRef,
    pr_number: int,
    snapshot: PullRequestSnapshot,
    pushed_head_sha: str | None,
    runner: AsyncCommandRunner | None = None,
    worktree_path: Path | None = None,
    fetch_remote: str = "origin",
) -> tuple[_PostPushReuseDisposition, PullRequestSnapshot]:
    """Decide whether post-push reuse may keep the existing PR.

    Open snapshots must still contain the pushed tip — exact head OID or a
    fast-forward descendant — with brief retries for forge propagation. An open
    head that never contains the tip after retries fails closed — a still-open
    PR owns the head branch, so a replacement cannot be opened safely.
    Merged-with-tip keeps; closed / merged-without-tip replace.
    """
    current = snapshot
    for attempt in range(_POST_PUSH_TIP_RETRY_ATTEMPTS):
        if current.lifecycle is PullRequestLifecycle.open:
            if not pushed_head_sha:
                # Local tip unknown: cannot prove exclusion; keep prior behavior.
                return _PostPushReuseDisposition.keep, current
            if await _pr_snapshot_contains_pushed_tip(
                snapshot=current,
                pushed_head_sha=pushed_head_sha,
                runner=runner,
                worktree_path=worktree_path,
                fetch_remote=fetch_remote,
            ):
                return _PostPushReuseDisposition.keep, current
            if attempt + 1 >= _POST_PUSH_TIP_RETRY_ATTEMPTS:
                return _PostPushReuseDisposition.fail_closed, current
            await asyncio.sleep(_POST_PUSH_TIP_RETRY_DELAY_SECONDS)
            current = await forge_client.fetch_pull_request_snapshot(
                repo=repo,
                pr_number=pr_number,
            )
            continue
        if (
            current.lifecycle is PullRequestLifecycle.merged
            and await _pr_snapshot_contains_pushed_tip(
                snapshot=current,
                pushed_head_sha=pushed_head_sha,
                runner=runner,
                worktree_path=worktree_path,
                fetch_remote=fetch_remote,
            )
        ):
            return _PostPushReuseDisposition.keep, current
        return _PostPushReuseDisposition.open_replacement, current
    return _PostPushReuseDisposition.fail_closed, current  # pragma: no cover


async def _fail_reuse_pr_state_lookup(
    self: Any,
    *,
    workspace_id: str,
    ws: Any,
    push_branch_name: str,
    audit_remote_branch: str,
    pr_number: int | None,
    evidence_operation: str,
    error_message: str,
    failure_message: str,
) -> None:
    """Fail closed when reuse cannot revalidate live PR state."""
    _log.error(
        "executor.pr_reuse_revalidate_failed",
        workspace_id=workspace_id,
        pr_number=pr_number,
        pr_url=ws.pr_url,
        error=error_message[:500],
        reason_code=_PR_STATE_LOOKUP_FAILED_REASON_CODE,
    )
    await self._record_executor_pr_audit_event(
        workspace_id,
        event_type=_AUDIT_PR_CREATED_EVENT,
        action="pr_create",
        outcome="failed",
        reason_code=_PR_STATE_LOOKUP_FAILED_REASON_CODE,
        branch_name=push_branch_name,
        remote_branch=audit_remote_branch,
        pr_number=pr_number,
        pr_url=ws.pr_url,
        source_head_sha=None,
        evidence={
            "operation": evidence_operation,
            "error_message": error_message.strip() or "<no output>",
        },
    )
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.pushing,
        failure_reason=FailureReason.infrastructure_failure,
        message=failure_message[:2000],
        reason_code=_PR_STATE_LOOKUP_FAILED_REASON_CODE,
    )


async def push_and_open_pr(
    self: Any,
    *,
    ws: Any,
    profile: Any,
    defaults: Any,
    workspace_id: str,
    worktree_path: Path,
) -> Any | None:
    """Push the validated branch and open (or reuse) its PR.

    Returns the created/reused PR result, or ``None`` when the workspace was
    already marked FAILED (the caller must stop). PR creation is forge-neutral:
    ``push_and_open`` does a plain ``git push`` and routes the PR-open step
    through the resolved ``ForgeClient`` (GitHub or Bitbucket Cloud).

    When ``ws.pr_url`` is set (e.g. a feature-PR retry that preserved the source
    PR at admission), the live forge snapshot is revalidated before reuse. A PR
    that merged or closed after admission is not reused: identity is cleared and
    a replacement PR is opened for the pushed tip. When the PR is still open but
    its head ref was renamed after admission, the persisted push target is
    updated to the live ``head_ref`` before reuse. After a reuse push, the live
    snapshot is fetched again: an open PR is kept only when its head OID equals
    the pushed tip or is a fast-forward descendant of it (briefly retried for
    forge propagation; otherwise fail closed); if the PR merged concurrently,
    reuse is kept only when the merged PR's head OID equals or descends from the
    pushed tip; otherwise identity is cleared and a replacement PR is opened. If
    ``pr_number`` cannot be resolved, or snapshot lookup fails (pre- or
    post-push), reuse fails closed (identity kept, workspace marked failed) so a
    still-open PR is not unlinked or duplicated.
    """
    pr_title = title_with_task_tag(ws.task_title, ws.task_tag)
    pr_body = _build_pr_body(ws, defaults=defaults)
    push_branch_name = ws.branch_name or f"awf/{workspace_id}"
    existing_pr_remote_branch = ws.remote_push_branch if ws.pr_url else None
    existing_pr_remote_url = _existing_pr_remote_push_url(ws) if ws.pr_url else None
    audit_remote_branch = existing_pr_remote_branch or push_branch_name

    try:
        if ws.pr_url:
            # Reuse candidates must revalidate live forge state: admission may
            # have persisted pr_url while the PR was open, then it merged before
            # this push. Skipping the forge client here would push to the old
            # head and hand the monitor a merged URL (ShortCircuitCompleted)
            # without the retry tip. Bitbucket reuse now requires forge API env
            # for that lifecycle check — same as monitor attachment.
            from awf.common.bitbucket_client import BitbucketClientError

            try:
                forge_client = make_forge_client(
                    concrete_forge_for_repo(profile.forge, ws.repo_url),
                    self._runner,
                )
            except BitbucketClientError as exc:
                _log.error(
                    "executor.pr_failed",
                    workspace_id=workspace_id,
                    operation=exc.operation,
                    returncode=exc.status if exc.status is not None else 0,
                    reason_code=exc.reason_code,
                )
                await self._record_executor_pr_audit_event(
                    workspace_id,
                    event_type=_AUDIT_PR_CREATED_EVENT,
                    action="pr_create",
                    outcome="failed",
                    reason_code=_PR_CREATE_FAILED_REASON_CODE,
                    branch_name=push_branch_name,
                    remote_branch=audit_remote_branch,
                    pr_number=None,
                    pr_url=None,
                    source_head_sha=None,
                    evidence={
                        "operation": exc.operation,
                        "returncode": exc.status if exc.status is not None else 0,
                        "error_message": exc.body.strip() or "<no output>",
                    },
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.pushing,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=str(exc)[:2000],
                    reason_code=exc.reason_code,
                )
                return None

            async with forge_client:
                pr_number = ws.pr_number or _extract_pr_number(ws.pr_url)
                if pr_number is None:
                    # Cannot revalidate without a number. Fail closed (keep
                    # identity) rather than clear-and-replace: an unresolved
                    # number is the same uncertainty as a failed lifecycle
                    # lookup, and clearing would unlink a still-open PR.
                    _log.error(
                        "executor.pr_reuse_revalidate_failed",
                        workspace_id=workspace_id,
                        pr_number=None,
                        pr_url=ws.pr_url,
                        error="could not resolve pr_number for reuse revalidation",
                        reason_code=_PR_STATE_LOOKUP_FAILED_REASON_CODE,
                    )
                    await self._record_executor_pr_audit_event(
                        workspace_id,
                        event_type=_AUDIT_PR_CREATED_EVENT,
                        action="pr_create",
                        outcome="failed",
                        reason_code=_PR_STATE_LOOKUP_FAILED_REASON_CODE,
                        branch_name=push_branch_name,
                        remote_branch=audit_remote_branch,
                        pr_number=None,
                        pr_url=ws.pr_url,
                        source_head_sha=None,
                        evidence={
                            "operation": "resolve_pr_number",
                            "error_message": ("could not resolve pr_number for reuse revalidation"),
                        },
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.pushing,
                        failure_reason=FailureReason.infrastructure_failure,
                        message=(
                            "Could not verify whether the existing pull request "
                            "is still open before reuse."
                        )[:2000],
                        reason_code=_PR_STATE_LOOKUP_FAILED_REASON_CODE,
                    )
                    return None

                try:
                    snapshot = await forge_client.fetch_pull_request_snapshot(
                        repo=RepoRef.from_url(ws.repo_url),
                        pr_number=pr_number,
                    )
                    reuse_existing = snapshot.lifecycle is PullRequestLifecycle.open
                    if not reuse_existing:
                        _log.info(
                            "executor.pr_reuse_abandoned",
                            workspace_id=workspace_id,
                            pr_number=pr_number,
                            pr_url=ws.pr_url,
                            lifecycle=str(snapshot.lifecycle),
                        )
                except (ForgeClientError, OSError, TimeoutError, ValueError) as exc:
                    _log.error(
                        "executor.pr_reuse_revalidate_failed",
                        workspace_id=workspace_id,
                        pr_number=pr_number,
                        pr_url=ws.pr_url,
                        error=str(exc)[:500],
                        reason_code=_PR_STATE_LOOKUP_FAILED_REASON_CODE,
                    )
                    await self._record_executor_pr_audit_event(
                        workspace_id,
                        event_type=_AUDIT_PR_CREATED_EVENT,
                        action="pr_create",
                        outcome="failed",
                        reason_code=_PR_STATE_LOOKUP_FAILED_REASON_CODE,
                        branch_name=push_branch_name,
                        remote_branch=audit_remote_branch,
                        pr_number=pr_number,
                        pr_url=ws.pr_url,
                        source_head_sha=None,
                        evidence={
                            "operation": "fetch_pull_request_snapshot",
                            "error_message": str(exc).strip() or "<no output>",
                        },
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.pushing,
                        failure_reason=FailureReason.infrastructure_failure,
                        message=(
                            "Could not verify whether the existing pull request "
                            "is still open before reuse."
                        )[:2000],
                        reason_code=_PR_STATE_LOOKUP_FAILED_REASON_CODE,
                    )
                    return None

                if reuse_existing:
                    live_head_ref = (
                        snapshot.head_ref.strip()
                        if isinstance(snapshot.head_ref, str) and snapshot.head_ref.strip()
                        else None
                    )
                    if live_head_ref is not None and live_head_ref != existing_pr_remote_branch:
                        _log.info(
                            "executor.pr_reuse_head_ref_synced",
                            workspace_id=workspace_id,
                            pr_number=pr_number,
                            pr_url=ws.pr_url,
                            previous_remote_branch=existing_pr_remote_branch,
                            live_head_ref=live_head_ref,
                        )
                        await _sync_reuse_remote_push_branch(
                            self,
                            workspace_id=workspace_id,
                            ws=ws,
                            live_head_ref=live_head_ref,
                        )
                        existing_pr_remote_branch = live_head_ref
                        audit_remote_branch = live_head_ref
                    pr = await self._pr_creator.push_and_open(
                        worktree_path=worktree_path,
                        branch_name=push_branch_name,
                        base_branch=ws.branch_base,
                        title=pr_title,
                        body=pr_body,
                        forge_client=None,
                        repo_url=ws.repo_url,
                        existing_pr_url=ws.pr_url,
                        remote_branch_name=existing_pr_remote_branch,
                        remote_url=existing_pr_remote_url,
                    )
                    # Pre-push open is not atomic with the push: the PR can merge
                    # or be force-pushed in between. Recheck tip containment before
                    # handing the monitor a URL that may exclude the retry tip.
                    try:
                        post_snapshot = await forge_client.fetch_pull_request_snapshot(
                            repo=RepoRef.from_url(ws.repo_url),
                            pr_number=pr_number,
                        )
                    except (ForgeClientError, OSError, TimeoutError, ValueError) as exc:
                        await _fail_reuse_pr_state_lookup(
                            self,
                            workspace_id=workspace_id,
                            ws=ws,
                            push_branch_name=push_branch_name,
                            audit_remote_branch=audit_remote_branch,
                            pr_number=pr_number,
                            evidence_operation="fetch_pull_request_snapshot_post_push",
                            error_message=str(exc),
                            failure_message=(
                                "Could not verify whether the reused pull request "
                                "still contains the pushed tip after reuse."
                            ),
                        )
                        return None
                    try:
                        disposition, post_snapshot = await _resolve_post_push_reuse(
                            forge_client=forge_client,
                            repo=RepoRef.from_url(ws.repo_url),
                            pr_number=pr_number,
                            snapshot=post_snapshot,
                            pushed_head_sha=pr.head_sha,
                            runner=self._runner,
                            worktree_path=worktree_path,
                            fetch_remote=existing_pr_remote_url or "origin",
                        )
                    except (ForgeClientError, OSError, TimeoutError, ValueError) as exc:
                        await _fail_reuse_pr_state_lookup(
                            self,
                            workspace_id=workspace_id,
                            ws=ws,
                            push_branch_name=push_branch_name,
                            audit_remote_branch=audit_remote_branch,
                            pr_number=pr_number,
                            evidence_operation=("fetch_pull_request_snapshot_post_push_tip_retry"),
                            error_message=str(exc),
                            failure_message=(
                                "Could not verify whether the reused pull request "
                                "still contains the pushed tip after reuse."
                            ),
                        )
                        return None
                    if disposition is _PostPushReuseDisposition.keep:
                        if post_snapshot.lifecycle is PullRequestLifecycle.merged:
                            _log.info(
                                "executor.pr_reuse_merged_tip_contained",
                                workspace_id=workspace_id,
                                pr_number=pr_number,
                                pr_url=ws.pr_url,
                                head_sha=pr.head_sha,
                            )
                    elif disposition is _PostPushReuseDisposition.fail_closed:
                        await _fail_reuse_pr_state_lookup(
                            self,
                            workspace_id=workspace_id,
                            ws=ws,
                            push_branch_name=push_branch_name,
                            audit_remote_branch=audit_remote_branch,
                            pr_number=pr_number,
                            evidence_operation="post_push_open_tip_mismatch",
                            error_message=(
                                "reused PR remained open but head_sha "
                                f"{post_snapshot.head_sha!r} does not contain "
                                f"pushed tip {pr.head_sha!r}"
                            ),
                            failure_message=(
                                "Reused pull request is still open but its head "
                                "no longer contains the pushed tip after reuse."
                            ),
                        )
                        return None
                    else:
                        _log.info(
                            "executor.pr_reuse_abandoned_post_push",
                            workspace_id=workspace_id,
                            pr_number=pr_number,
                            pr_url=ws.pr_url,
                            lifecycle=str(post_snapshot.lifecycle),
                            pushed_head_sha=pr.head_sha,
                            merged_head_sha=post_snapshot.head_sha,
                        )
                        await _clear_stale_pr_identity_for_replacement(
                            self,
                            workspace_id=workspace_id,
                            ws=ws,
                        )
                        audit_remote_branch = push_branch_name
                        pr = await self._pr_creator.push_and_open(
                            worktree_path=worktree_path,
                            branch_name=push_branch_name,
                            base_branch=ws.branch_base,
                            title=pr_title,
                            body=pr_body,
                            forge_client=forge_client,
                            repo_url=ws.repo_url,
                            existing_pr_url=None,
                            remote_branch_name=None,
                            remote_url=existing_pr_remote_url,
                        )
                else:
                    await _clear_stale_pr_identity_for_replacement(
                        self,
                        workspace_id=workspace_id,
                        ws=ws,
                    )
                    audit_remote_branch = push_branch_name
                    pr = await self._pr_creator.push_and_open(
                        worktree_path=worktree_path,
                        branch_name=push_branch_name,
                        base_branch=ws.branch_base,
                        title=pr_title,
                        body=pr_body,
                        forge_client=forge_client,
                        repo_url=ws.repo_url,
                        existing_pr_url=None,
                        remote_branch_name=None,
                        remote_url=existing_pr_remote_url,
                    )
        else:
            # New PR: ``make_forge_client`` builds the Bitbucket client eagerly
            # via ``from_env()``, so a missing/invalid Bitbucket API env raises
            # ``BitbucketClientError`` here — before ``push_and_open`` runs the
            # git push or the create-PR call, so it cannot be wrapped as a
            # ``PullRequestError`` downstream. Map it onto the same
            # PR_CREATE_FAILED audit event + evidence that a ``create_pull_request``
            # failure records (instead of falling through to the opaque
            # "unexpected error" handler that emits no PR audit event), then fail
            # the run. No git_push-succeeded event is recorded because the push
            # never ran. ``BitbucketClientError`` is imported lazily so the
            # GitHub-only hot path never pays for the httpx import it drags in
            # (mirrors forge.make_forge_client / pr_creator). ``async with``
            # releases the Bitbucket httpx pool deterministically (GitHub aclose
            # is a no-op).
            from awf.common.bitbucket_client import BitbucketClientError

            try:
                forge_client = make_forge_client(
                    concrete_forge_for_repo(profile.forge, ws.repo_url),
                    self._runner,
                )
            except BitbucketClientError as exc:
                _log.error(
                    "executor.pr_failed",
                    workspace_id=workspace_id,
                    operation=exc.operation,
                    returncode=exc.status if exc.status is not None else 0,
                    reason_code=exc.reason_code,
                )
                await self._record_executor_pr_audit_event(
                    workspace_id,
                    event_type=_AUDIT_PR_CREATED_EVENT,
                    action="pr_create",
                    outcome="failed",
                    reason_code=_PR_CREATE_FAILED_REASON_CODE,
                    branch_name=push_branch_name,
                    remote_branch=audit_remote_branch,
                    pr_number=None,
                    pr_url=None,
                    source_head_sha=None,
                    evidence={
                        "operation": exc.operation,
                        "returncode": exc.status if exc.status is not None else 0,
                        "error_message": exc.body.strip() or "<no output>",
                    },
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.pushing,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=str(exc)[:2000],
                    # Preserve the forge-specific reason code (e.g.
                    # BITBUCKET_AUTH_NOT_CONFIGURED) so the failed workspace
                    # carries actionable doctor guidance, matching the
                    # PullRequestError path below instead of falling back to
                    # the generic INFRASTRUCTURE_FAILURE.
                    reason_code=exc.reason_code,
                )
                return None
            async with forge_client:
                pr = await self._pr_creator.push_and_open(
                    worktree_path=worktree_path,
                    branch_name=push_branch_name,
                    base_branch=ws.branch_base,
                    title=pr_title,
                    body=pr_body,
                    forge_client=forge_client,
                    repo_url=ws.repo_url,
                    existing_pr_url=None,
                    remote_branch_name=existing_pr_remote_branch,
                    remote_url=existing_pr_remote_url,
                )
    except PullRequestError as exc:
        _log.error(
            "executor.pr_failed",
            workspace_id=workspace_id,
            operation=exc.operation,
            returncode=exc.returncode,
            reason_code=exc.reason_code,
        )
        if exc.operation != "git push":
            await self._record_executor_pr_audit_event(
                workspace_id,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="git_push",
                outcome="succeeded",
                reason_code="PR_UPDATED" if ws.pr_url else "PR_OPENED",
                branch_name=push_branch_name,
                remote_branch=audit_remote_branch,
                pr_number=_extract_pr_number(ws.pr_url) if ws.pr_url else None,
                pr_url=ws.pr_url,
                source_head_sha=exc.head_sha,
            )
        evidence = {
            "operation": exc.operation,
            "returncode": exc.returncode,
            "error_message": exc.stderr.strip() or "<no output>",
        }
        if exc.details is not None:
            evidence["details"] = exc.details
        await self._record_executor_pr_audit_event(
            workspace_id,
            event_type=(
                _AUDIT_GIT_PUSH_EVENT if exc.operation == "git push" else _AUDIT_PR_CREATED_EVENT
            ),
            action="git_push" if exc.operation == "git push" else "pr_create",
            outcome="failed",
            reason_code=(
                _GIT_PUSH_FAILED_REASON_CODE
                if exc.operation == "git push"
                else _PR_CREATE_FAILED_REASON_CODE
            ),
            branch_name=push_branch_name,
            remote_branch=audit_remote_branch,
            pr_number=_extract_pr_number(ws.pr_url) if ws.pr_url else None,
            pr_url=ws.pr_url,
            source_head_sha=exc.head_sha,
            evidence=evidence,
        )
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.pushing,
            failure_reason=FailureReason.infrastructure_failure,
            message=str(exc)[:2000],
            # Preserve a forge-specific reason code (e.g. Bitbucket auth /
            # rate-limit / transport) so the failed workspace carries the
            # actionable doctor guidance; ``None`` (git push, GitHub, no-URL)
            # falls back to ``INFRASTRUCTURE_FAILURE``.
            reason_code=exc.reason_code,
        )
        return None
    except Exception as exc:
        _log.exception("executor.pr_unexpected_failed", workspace_id=workspace_id)
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.pushing,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"unexpected error during PR creation: {exc!r}"[:2000],
        )
        return None
    return pr
