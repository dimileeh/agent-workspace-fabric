"""Best-effort operator/human PR notifications for monitor pauses.

Extracted from the decision loop so the loop module stays under the
maintainability line limit; behavior is unchanged. These helpers surface a
workflow-scope failure hint or a post-PR protected-scope pause as a PR comment
without letting a forge fault escape and abort the surrounding pause handling.
"""

from __future__ import annotations

from typing import Any

from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import (
    RepoRef,
)
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _redact_and_truncate_forge_error,
)
from awf.runtime.pr_monitor_runner.logging import _log


async def _post_workflow_scope_notification_best_effort(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    blocker_reason: str,
) -> None:
    """Post the human hint without blocking workflow-scope failure handling."""
    try:
        await self._post_human_notification_once(
            repo=repo,
            pr_number=pr_number,
            status=status,
            state=state,
            blocker_reason=blocker_reason,
        )
    except ForgeClientError as exc:
        # A Bitbucket workspace posts the human hint through ``BitbucketClient``,
        # whose ``post_comment`` raises ``BitbucketClientError`` (not
        # ``GitHubClientError``). Catch it alongside the GitHub error so a
        # transient or permanent comment failure degrades to a logged warning
        # here too, instead of escaping this best-effort helper and aborting the
        # surrounding workflow-scope failure handling.
        _log.warning(
            "monitor.workflow_scope_notification_failed",
            workspace_id=workspace_id,
            pr_number=pr_number,
            head_sha=status.head_sha[:10],
            error=_redact_and_truncate_forge_error(str(exc)),
        )


async def _post_protected_block_notification_best_effort(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    state: MonitorState,
    push_result: Any,
) -> None:
    """Post the operator block notification as a PR comment, deduped by epoch.

    A post-PR protected-scope pause preserves the offending commit and waits for
    an operator. Surface that on the PR so a human knows it is blocked (not
    silently failed). Deduped on the block epoch + violation content so a SECOND,
    different violation (or changed directive) re-notifies, while an identical
    re-evaluation within the same epoch stays quiet. Best-effort: a forge comment
    fault degrades to a logged warning and never escapes the pause handling."""
    from awf.runtime.pr_monitor_runner.helpers import _protected_block_notification_key

    details = push_result.details or {}
    block_epoch = details.get("block_epoch") if isinstance(details, dict) else None
    paths = details.get("paths", []) if isinstance(details, dict) else []
    protected_patterns = details.get("protected_patterns", []) if isinstance(details, dict) else []
    key = _protected_block_notification_key(
        block_epoch=block_epoch if isinstance(block_epoch, int) else None,
        paths=paths if isinstance(paths, list) else [],
        protected_patterns=protected_patterns if isinstance(protected_patterns, list) else [],
    )
    if state.threads_addressed_ids.get(key) == "notified":
        _log.info(
            "monitor.protected_block_notification_already_posted",
            workspace_id=workspace_id,
            pr_number=pr_number,
            block_epoch=block_epoch,
        )
        return
    path_list = ", ".join(paths) if isinstance(paths, list) and paths else "protected file(s)"
    body = (
        f"AWF paused PR #{pr_number} for an operator decision.\n\n"
        f"A monitor repair commit changed protected quality-gate file(s) outside the "
        f"workspace's declared ownership: {path_list}. The commit is **preserved** "
        f"(not reverted) and the workspace is now `blocked` awaiting an operator.\n\n"
        f"Resolve with `awf workspace guide`:\n"
        f"- approve and keep the change: `--grant <path> --approve-policy-downgrade`\n"
        f'- revert the change: `--directive "revert the protected-file edit"`'
    )
    try:
        await self._deps.gh.post_comment(repo=repo, pr_number=pr_number, body=body)
    except ForgeClientError as exc:
        _log.warning(
            "monitor.protected_block_notification_failed",
            workspace_id=workspace_id,
            pr_number=pr_number,
            block_epoch=block_epoch,
            error=_redact_and_truncate_forge_error(str(exc)),
        )
        return
    state.mark_addressed(key, "notified")


async def _handle_paused_into_blocked(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    state: MonitorState,
    push_result: Any,
) -> bool:
    """Finalize a post-PR protected-scope pause: notify + persist, stop cleanly.

    The workspace is already ``blocked`` (the push-repair transitioned it). Post
    the epoch-deduped operator notification and persist ``MonitorState`` so any
    review comments addressed/known at block time survive the pause, then return
    True to stop the monitor cycle WITHOUT marking the workspace failed — it
    resumes only on an operator decision."""
    await _post_protected_block_notification_best_effort(
        self,
        workspace_id=workspace_id,
        repo=repo,
        pr_number=pr_number,
        state=state,
        push_result=push_result,
    )
    await self._persist_state(workspace_id, state)
    return True
