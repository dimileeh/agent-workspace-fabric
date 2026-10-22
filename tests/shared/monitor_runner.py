"""Shared PR-monitor runner test doubles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from awf.common.commands import AsyncCommandRunner
from awf.common.forge_thread_audit import ReviewThreadAudit
from awf.common.github_client import GitHubClient, RepoRef
from awf.node.compose_manager import ComposeTeardownResult
from awf.runtime.pr_monitor import MonitorState
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

    async def reply_to_thread(self, *, thread_id: str, body: str) -> str:
        """Record the disposition reply instead of shelling ``gh``."""
        self.thread_replies.append((thread_id, body))
        return f"reply-{len(self.thread_replies)}"

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int
    ) -> ReviewThreadAudit:
        """A clean audit (no AWF-resolved threads) for tests that do not exercise the gate."""
        del repo, pr_number
        return ReviewThreadAudit(supported=True, viewer_login="awf-test-bot")

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
        state: MonitorState, item_id: str, result: VerdictResult | MonitorVerdictResult
    ) -> None:
        real_sync(state, item_id, result)
        disposition = read_item_disposition(state, item_id)
        if (
            disposition is not None
            and disposition.disposition_class == "fixed"
            and not disposition.commits
        ):
            record_item_disposition(state, replace(disposition, commits=(sha,)))

    monkeypatch.setattr(_comments, "sync_item_disposition", _sync)
