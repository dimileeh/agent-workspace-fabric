"""WorkspaceExecutor push + open-PR step.

Mechanically extracted from ``execution_flow.execute``; behavior is unchanged.
Keeping the forge-neutral ``git push`` + PR-open call and its audit/failure
handling here keeps ``execution_flow`` focused on the agent-run → commit →
validate → push pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.common.forge import concrete_forge_for_repo, make_forge_client
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
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.runtime.pr_creator import PullRequestError


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
    """
    pr_title = title_with_task_tag(ws.task_title, ws.task_tag)
    pr_body = _build_pr_body(ws, defaults=defaults)
    push_branch_name = ws.branch_name or f"awf/{workspace_id}"
    existing_pr_remote_branch = ws.remote_push_branch if ws.pr_url else None
    existing_pr_remote_url = _existing_pr_remote_push_url(ws) if ws.pr_url else None
    audit_remote_branch = existing_pr_remote_branch or push_branch_name

    try:
        if ws.pr_url:
            # Reuse path: ``push_and_open`` only does a plain ``git push`` and
            # reuses the existing PR — it never touches the forge client. Skip
            # resolving one so a Bitbucket reuse push is not gated on forge API
            # env: ``make_forge_client`` builds ``BitbucketClient`` eagerly via
            # ``from_env()``, which would fail the run on missing/invalid
            # Bitbucket API env before the push, even though reuse makes no forge
            # API call. (This mirrors the pre-forge-client flow, where reuse
            # never resolved a forge client.)
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
            evidence={
                "operation": exc.operation,
                "returncode": exc.returncode,
                "error_message": exc.stderr.strip() or "<no output>",
            },
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
