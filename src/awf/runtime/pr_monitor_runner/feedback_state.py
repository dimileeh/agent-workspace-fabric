"""Extracted PullRequestMonitorRunner domain operations.

This module contains mechanically moved methods from ``awf.runtime.pr_monitor_runner.runner`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import os as os
import re as re
import time as time
from typing import Any, cast

from awf.common.bitbucket_client import BitbucketClient
from awf.common.github_client import GitHubClient, RepoRef
from awf.db.repositories import (
    PRFeedbackResolutionRepository,
    pr_feedback_body_hash,
)
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
    ReviewComment,
)
from awf.runtime.pr_monitor_runner.comments import (
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _drop_stale_review_comment_addressed_state,
    _drop_stale_review_thread_addressed_state,
    _mark_review_comment_addressed,
    _monitor_state_verdict,
    _review_comment_needs_attention,
    _review_comment_resolution_body,
)


def _forge_scm_provider(self: Any) -> str:
    """Return the SCM provider key for the runner's resolved forge client.

    The PR-feedback provenance/replay rows are keyed by ``scm_provider`` so a
    GitHub and a Bitbucket PR with the same numeric id never alias. ``self._deps.gh``
    is the per-workspace resolved ``ForgeClient`` (a ``BitbucketClient`` or a
    ``GitHubClient``), so it is the authoritative forge signal here — no DB re-read.

    GitHub and Bitbucket are the only forges today. A blanket ``else "github"``
    fallback would silently store (and later query) any future third forge's rows
    under the GitHub key, aliasing its feedback state against GitHub workspaces on
    the same repo/PR number. Fail loudly on an unknown client instead, so a newly
    wired forge is forced to declare its own provider key here.
    """
    if isinstance(self._deps.gh, BitbucketClient):
        return "bitbucket"
    if isinstance(self._deps.gh, GitHubClient):
        return "github"
    raise NotImplementedError(f"unknown forge client type: {type(self._deps.gh).__name__}")


def _forge_pr_url(self: Any, repo: RepoRef, pr_number: int) -> str:
    """Build the forge-correct PR web URL for provenance rows.

    A hardcoded github.com URL would poison Bitbucket provenance/replay state with a
    nonexistent github.com link; derive the host/path shape from the resolved forge.
    Mirrors ``_forge_scm_provider``: fail loudly on an unknown client so a newly wired
    forge is forced to declare its URL shape here, rather than silently persisting a
    nonexistent github.com link in provenance rows.
    """
    if isinstance(self._deps.gh, BitbucketClient):
        return f"https://bitbucket.org/{repo.slug()}/pull-requests/{pr_number}"
    if isinstance(self._deps.gh, GitHubClient):
        return f"https://github.com/{repo.slug()}/pull/{pr_number}"
    raise NotImplementedError(f"unknown forge client type: {type(self._deps.gh).__name__}")


async def _record_pr_feedback_resolution(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    pr_head_sha: str,
    comment: ReviewComment,
    verdict_result: VerdictResult,
    operation_id: str | None,
) -> None:
    if verdict_result.verdict == "agent_failed":
        return
    # One-time state loss on upgrade (harmless): before #454 every workspace wrote
    # ``scm_provider="github"``, so any Bitbucket feedback row recorded pre-upgrade
    # is keyed under "github" and a restarted Bitbucket workspace (now querying under
    # "bitbucket") won't find it. The thread is genuinely resolved on Bitbucket, so
    # ``fetch_pr_status`` won't re-surface it — at worst one redundant verdict gets
    # re-recorded under the correct provider; no migration is required.
    async with self._deps.session_factory() as s:
        await PRFeedbackResolutionRepository(s).record_resolution(
            scm_provider=_forge_scm_provider(self),
            repository_key=repo.slug(),
            pull_request_key=str(pr_number),
            pull_request_url=_forge_pr_url(self, repo, pr_number),
            head_sha=pr_head_sha,
            feedback_kind="review_comment",
            feedback_id=comment.comment_id,
            feedback_body=_review_comment_resolution_body(comment),
            feedback_author=comment.author,
            feedback_url=comment.url,
            verdict=verdict_result.verdict,
            reason=verdict_result.reason,
            source_workspace_id=workspace_id,
            source_operation_id=operation_id,
        )
        await s.commit()


async def _refresh_pr_feedback_resolution_state(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
) -> bool:
    changed = await self._apply_pr_feedback_resolution_state(
        workspace_id=workspace_id,
        repo=repo,
        pr_number=pr_number,
        status=status,
        state=state,
    )
    if _drop_stale_review_thread_addressed_state(status, state):
        changed = True
    if _drop_stale_review_comment_addressed_state(status, state):
        changed = True
    return cast(bool, changed)


async def _apply_pr_feedback_resolution_state(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
) -> bool:
    del workspace_id
    if not status.unresolved_review_comments:
        return False
    async with self._deps.session_factory() as s:
        rows = await PRFeedbackResolutionRepository(s).list_for_pr(
            scm_provider=_forge_scm_provider(self),
            repository_key=repo.slug(),
            pull_request_key=str(pr_number),
        )
    if not rows:
        return False
    rows_by_feedback = {
        (row.feedback_kind, row.feedback_id, row.feedback_body_hash): row for row in rows
    }
    changed = False
    for comment in status.unresolved_review_comments:
        if not _review_comment_needs_attention(state, comment):
            continue
        row = rows_by_feedback.get(
            (
                "review_comment",
                comment.comment_id,
                pr_feedback_body_hash(_review_comment_resolution_body(comment)),
            )
        )
        if row is None:
            continue
        _mark_review_comment_addressed(state, comment, _monitor_state_verdict(row.verdict))
        changed = True
    return changed
