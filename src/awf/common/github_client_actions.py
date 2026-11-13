"""GitHub Actions workflow-run settlement for a PR head (#1230).

Bound onto :class:`~awf.common.github_client.GitHubClient` as a method
(``fetch_actions_runs_state = github_client_actions.fetch_actions_runs_state``) so
the client module stays under the first-party file-size guardrail.
``GitHubClientError`` is imported inside the function because ``github_client``
imports this module at load time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.common.github_retry import RetryPolicy
from awf.runtime.pr_monitor_models import ActionsRunsState

if TYPE_CHECKING:
    from awf.common.github_client import GitHubClient, RepoRef

_OPERATION = "list_actions_runs_for_sha"


async def fetch_actions_runs_state(
    self: GitHubClient, *, repo: RepoRef, head_sha: str, retry: bool = True
) -> ActionsRunsState:
    """Return whether every GitHub Actions workflow run for ``head_sha`` completed.

    The ``statusCheckRollup`` omits a workflow run that is still queued with zero
    jobs, so ``fetch_pr_status`` alone can report a green head whose CI has not
    started. ``gh run list`` lists only Actions workflow runs; other apps' check
    suites never appear, so they cannot block. Any status other than
    ``completed`` is unsettled (fail-safe for future values). An empty list (no
    Actions on this repo or head) is ``SETTLED``. A transport or shape error
    raises ``GitHubClientError``; the caller decides how to wait. ``retry=False``
    (the pre-merge recheck) fails fast, as for ``fetch_pr_status``.
    """
    from awf.common.github_client import GitHubClientError

    runs = await self._gh_json(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo.slug(),
            "--commit",
            head_sha,
            "--json",
            "databaseId,status",
            "--limit",
            "100",
        ],
        operation=_OPERATION,
        retry_policy=RetryPolicy.READ if retry else RetryPolicy.NEVER,
    )
    if runs is None:
        return ActionsRunsState.SETTLED
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise GitHubClientError(
            operation=f"{_OPERATION} (shape)",
            returncode=0,
            stderr=f"expected a list of workflow runs, got: {str(runs)[:200]}",
        )
    if any(str(run.get("status") or "").lower() != "completed" for run in runs):
        return ActionsRunsState.PENDING
    return ActionsRunsState.SETTLED
