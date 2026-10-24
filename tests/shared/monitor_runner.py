"""Shared PR-monitor runner test doubles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from awf.common.commands import AsyncCommandRunner
from awf.common.forge_thread_audit import ReviewThreadAudit
from awf.common.github_client import GitHubClient, RepoRef
from awf.common.github_client_threads import _audited_thread
from awf.common.github_retry import RetryPolicy
from awf.node.compose_manager import ComposeTeardownResult
from awf.runtime.pr_monitor import MonitorState, PRStatus, ReviewThread
from awf.runtime.pr_monitor_runner import comments as _comments
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    MonitorVerdictResult,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.thread_disposition import (
    read_item_disposition,
    record_item_disposition,
)


class DefaultMergeMethodGitHubClient(GitHubClient):
    """GitHub client test double that keeps legacy merge queues stable.

    It also records disposition replies and serves a clean review-thread audit
    (#1018) so runner tests keep stable ``FakeCommandRunner`` queues.
    """

    def __init__(self, runner: AsyncCommandRunner, **kwargs: Any) -> None:
        super().__init__(runner, **kwargs)
        self.thread_replies: list[tuple[str, str]] = []
        self.review_threads: dict[str, dict[str, Any]] = {}

    def _review_thread(self, thread_id: str) -> dict[str, Any]:
        return self.review_threads.setdefault(
            thread_id,
            {
                "id": thread_id,
                "isResolved": False,
                "isOutdated": False,
                "comments": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            },
        )

    def remember_review_threads(self, threads: tuple[ReviewThread, ...]) -> None:
        """Keep observed comments in order, retaining replies across status refreshes."""
        for thread in threads:
            node = self._review_thread(thread.thread_id)
            node.update(isResolved=thread.is_resolved, isOutdated=thread.is_outdated)
            comments = node["comments"]["nodes"]
            by_id = {comment["id"]: comment for comment in comments}
            for comment in thread.comments:
                current = by_id.get(comment.comment_id)
                if current is None:
                    current = {"id": comment.comment_id}
                    comments.append(current)
                current.update(
                    body=comment.body,
                    bodyText=comment.body,
                    viewerDidAuthor=comment.viewer_did_author,
                    createdAt=comment.created_at.isoformat() if comment.created_at else None,
                    lastEditedAt=(
                        comment.updated_at.isoformat()
                        if comment.updated_at and comment.updated_at != comment.created_at
                        else None
                    ),
                )

    async def fetch_pr_status(
        self, *, repo: RepoRef, pr_number: int, base_behind_count: int, retry: bool = True
    ) -> PRStatus:
        status = await super().fetch_pr_status(
            repo=repo, pr_number=pr_number, base_behind_count=base_behind_count, retry=retry
        )
        self.remember_review_threads(
            status.unresolved_inline_threads + status.outdated_unresolved_inline_threads
        )
        return status

    async def reply_to_thread(self, *, thread_id: str, body: str) -> str:
        """Record the disposition reply instead of shelling ``gh``."""
        self.thread_replies.append((thread_id, body))
        reply_id = f"reply-{len(self.thread_replies)}"
        self._review_thread(thread_id)["comments"]["nodes"].append(
            {
                "id": reply_id,
                "body": body,
                "bodyText": body,
                "viewerDidAuthor": True,
                "createdAt": datetime.now(UTC).isoformat(),
            }
        )
        return reply_id

    async def resolve_thread(self, *, thread_id: str) -> None:
        """Preserve queued mutation failures and reflect successful resolution in audits."""
        await super().resolve_thread(thread_id=thread_id)
        self._review_thread(thread_id).update(isResolved=True, resolvedBy={"login": "awf-test-bot"})

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int, retry: bool = True, thread_id: str | None = None
    ) -> ReviewThreadAudit:
        """Audit the fixture's observed comments and actual recorded replies."""
        del repo, pr_number, retry
        threads = []
        for tid, node in self.review_threads.items():
            if thread_id is None or tid == thread_id:
                thread = await _audited_thread(self, node, retry_policy=RetryPolicy.NEVER)
                assert thread is not None
                threads.append(thread)
        return ReviewThreadAudit(
            supported=True, viewer_login="awf-test-bot", threads=tuple(threads)
        )

    async def fetch_repo_merge_methods(self, *, repo: RepoRef) -> tuple[str, ...]:
        """Return all legacy repository merge methods for older runner tests."""
        del repo
        return ("merge", "squash", "rebase")

    async def fetch_branch_pull_request_allowed_merge_methods(
        self,
        *,
        repo: RepoRef,
        branch: str,
    ) -> tuple[str, ...] | None:
        """Return no branch-level merge-method constraint for older tests."""
        del repo, branch
        return None


def mock_completed_compose_manager(
    result: ComposeTeardownResult | None = None,
    *,
    exc: Exception | None = None,
    on_teardown: Callable[[], None] | None = None,
) -> tuple[object, list[tuple[str, Path, str, bool]]]:
    """Patch completed-workspace compose teardown and record teardown calls."""
    calls: list[tuple[str, Path, str, bool]] = []
    resolved_result = result or ComposeTeardownResult(
        status="succeeded",
        reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
    )

    class _FakeComposeManager:
        def __init__(self, *, work_dir: Path, template_path: Path) -> None:
            self.work_dir = work_dir
            self.template_path = template_path

        async def teardown_project(
            self,
            *,
            project_name: str,
            compose_file: Path,
            workspace_id: str,
            remove_volumes: bool = True,
        ) -> ComposeTeardownResult:
            calls.append((project_name, compose_file, workspace_id, remove_volumes))
            if on_teardown is not None:
                on_teardown()
            if exc is not None:
                raise exc
            return resolved_result

    return patch(
        "awf.runtime.pr_monitor_runner.lifecycle.ComposeManager",
        new=_FakeComposeManager,
    ), calls


def supply_fixed_item_commit(monkeypatch: pytest.MonkeyPatch, sha: str = "c" * 40) -> None:
    """Give FIXED dispositions the commit a real worktree's #935 probe records.

    Runner tests that drive the real verdict path without a git worktree never
    record item commit provenance, which is where a FIXED disposition gets the
    commit its reply cites — so the thread would (correctly) park as
    ``THREAD_DISPOSITION_EVIDENCE_MISSING``. Suites that test other behaviour use
    this to supply that commit; the reply-before-resolve path itself stays real.
    """
    real_sync = _comments.sync_item_disposition

    def _sync(
        state: MonitorState,
        item_id: str,
        result: VerdictResult | MonitorVerdictResult,
        *,
        thread: ReviewThread | None = None,
    ) -> None:
        real_sync(state, item_id, result, thread=thread)
        disposition = read_item_disposition(state, item_id)
        if (
            disposition is not None
            and disposition.disposition_class == "fixed"
            and not disposition.commits
        ):
            record_item_disposition(state, replace(disposition, commits=(sha,)))

    monkeypatch.setattr(_comments, "sync_item_disposition", _sync)
