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

from awf.db.repositories import (
    PRFeedbackResolutionRepository,
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
from awf.runtime.pr_monitor_runner.shared import (
    MonitorState,
    PRStatus,
    RepoRef,
    ReviewComment,
    pr_feedback_body_hash,
)


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
    async with self._deps.session_factory() as s:
        await PRFeedbackResolutionRepository(s).record_resolution(
            scm_provider="github",
            repository_key=repo.slug(),
            pull_request_key=str(pr_number),
            pull_request_url=f"https://github.com/{repo.slug()}/pull/{pr_number}",
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
            scm_provider="github",
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
