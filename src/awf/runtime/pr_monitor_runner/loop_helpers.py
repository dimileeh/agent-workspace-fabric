"""Pull request monitor loop helper functions."""

from __future__ import annotations

from typing import Any

from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import MonitorState, PRStatus
from awf.runtime.pr_monitor_runner.helpers import _redact_and_truncate_forge_error
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
