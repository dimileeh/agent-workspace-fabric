"""Operator acknowledgement of unverified AWF resolutions (#1018).

A thread the disposition gate flags (resolved by AWF's identity, no marker) is
acknowledged through the existing operator guide path: a guide that names the
flagged thread id records an ``operator_acknowledged`` disposition, and the next
gate run posts that acknowledgement — with its marker, without the directive
text — into the resolved thread, after which the cert can follow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import AuditedReviewThread, ReviewThreadAudit
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _disposition_unverified_key
from awf.runtime.pr_monitor import MonitorState, OperatorHint
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint,
)
from awf.runtime.pr_monitor_runner.thread_disposition import read_item_disposition
from awf.runtime.pr_monitor_runner.thread_disposition_gate import (
    review_thread_disposition_gate,
)
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

pytestmark = pytest.mark.unit

_REPO = RepoRef(owner="acme", name="widgets")
_SILENT = "PRRT_test_silent_p1"
_DIRECTIVE = f"Acknowledge {_SILENT}: handled in a follow-up issue, ping @maintainer"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _SilentAuditForge(DefaultMergeMethodGitHubClient):
    """One thread resolved by AWF's identity with no AWF marker in it."""

    def __init__(self, runner: FakeCommandRunner, *, reply_error: ForgeClientError | None = None):
        super().__init__(runner)
        self.reply_error = reply_error

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int
    ) -> ReviewThreadAudit:
        del repo, pr_number
        return ReviewThreadAudit(
            supported=True,
            viewer_login="awf-operator",
            threads=(
                AuditedReviewThread(
                    thread_id=_SILENT,
                    is_resolved=True,
                    is_outdated=False,
                    resolved_by="awf-operator",
                ),
            ),
        )

    async def reply_to_thread(self, *, thread_id: str, body: str) -> str:
        if self.reply_error is not None:
            raise self.reply_error
        return await super().reply_to_thread(thread_id=thread_id, body=body)


async def _runner(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, gh: Any
) -> tuple[str, Any]:
    workspace_id = await seed_monitoring_workspace(factory, auto_merge=False)
    runner = make_runner(
        factory=factory,
        cmd=gh._runner,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
        gh=gh,
    )
    return workspace_id, runner


async def _gate(runner: Any, workspace_id: str, state: MonitorState) -> Any:
    return await review_thread_disposition_gate(
        runner,
        workspace_id=workspace_id,
        repo=_REPO,
        pr_number=42,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        monitor_log=None,
    )


def _guide(directive: str) -> OperatorHint:
    return OperatorHint(
        reason="guide",
        directive=directive,
        operation_id="op_test_1",
        reason_code="OPERATOR_GUIDE",
    )


async def test_operator_guide_acknowledges_flagged_thread_and_cert_follows(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()

    blocked = await _gate(runner, workspace_id, state)
    assert blocked.blocker is not None and _SILENT in blocked.blocker
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]

    _finalize_processed_operator_hint(state, hint=_guide(_DIRECTIVE))
    ack = read_item_disposition(state, _SILENT)
    assert ack is not None and ack.disposition_class == "operator_acknowledged"
    assert _disposition_unverified_key(_SILENT) not in state.threads_addressed_ids

    passed = await _gate(runner, workspace_id, state)

    assert passed.blocker is None
    assert passed.count_rows is not None
    assert dict(passed.count_rows)["operator acknowledged"] == 1
    assert len(gh.thread_replies) == 1
    thread_id, body = gh.thread_replies[0]
    assert thread_id == _SILENT
    assert body.splitlines()[0] == (
        f"<!-- AWF:thread-disposition:v1 class=operator_acknowledged thread={_SILENT} -->"
    )
    assert "op_test_1" in body
    assert "follow-up issue" not in body  # the directive text is never published
    assert "@" not in body

    # The forge may still serve the pre-reply audit: the recorded reply is reused.
    again = await _gate(runner, workspace_id, state)
    assert again.blocker is None
    assert len(gh.thread_replies) == 1


async def test_acknowledgement_reply_failure_keeps_the_thread_blocked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _SilentAuditForge(
        FakeCommandRunner(),
        reply_error=GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502"),
    )
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()
    await _gate(runner, workspace_id, state)
    _finalize_processed_operator_hint(state, hint=_guide(_DIRECTIVE))

    result = await _gate(runner, workspace_id, state)

    assert result.blocker is not None
    assert result.blocker.startswith("REVIEW_THREAD_DISPOSITION_UNVERIFIED:")
    assert _SILENT in result.blocker
    assert gh.thread_replies == []
    ack = read_item_disposition(state, _SILENT)
    assert ack is not None and ack.reply_comment_id is None


async def test_guide_naming_unflagged_thread_records_nothing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _SilentAuditForge(FakeCommandRunner())
    workspace_id, runner = await _runner(factory, tmp_path, gh)
    state = MonitorState()

    _finalize_processed_operator_hint(state, hint=_guide(_DIRECTIVE))
    assert read_item_disposition(state, _SILENT) is None

    result = await _gate(runner, workspace_id, state)
    assert result.blocker is not None
    assert gh.thread_replies == []
