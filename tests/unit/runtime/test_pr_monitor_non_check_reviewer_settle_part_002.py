"""Continuation of non-check async reviewer settle execute-path tests.

Split out of ``test_pr_monitor_non_check_reviewer_settle.py`` to keep each
test module under the first-party line-count guardrail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import OperationRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import Merge, MonitorState, NotifyHuman
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_non_check_reviewer_settle import (
    REPO_URL,
    _ready_status,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_execute_merge_wait_operation_payload_includes_activity_countdown(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    anchor = datetime.now(UTC)
    ws_id = await seed_monitoring_workspace(factory, pr_number=192, head_sha="head-a")
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=900,
        non_check_reviewer_logins=("greptile-apps",),
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=ws_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=192,
        status=_ready_status(
            pr_number=192,
            head_sha="head-a",
            quiet_period_anchor_at=anchor,
            quiet_period_anchor_source="review_comment",
            latest_external_review_activity_at=anchor,
            latest_external_review_activity_source="review_comment",
        ),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=ws_id)
    payload = operations[0].payload
    assert payload["action"] == "reviewer_settle_wait"
    assert payload["activity_anchor_at"] == anchor.isoformat()
    assert payload["activity_anchor_source"] == "review_comment"
    assert payload["quiet_until"] == (anchor + timedelta(seconds=900)).isoformat()
    assert 0 < payload["remaining_seconds"] <= 900
    assert payload["latest_external_review_activity_at"] == anchor.isoformat()


@pytest.mark.unit
async def test_pre_merge_status_refresh_rearms_non_check_reviewer_settle(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    old_anchor = datetime(2026, 5, 6, 10, 0, tzinfo=UTC)
    late_anchor = datetime.now(UTC)
    late_anchor_text = late_anchor.isoformat().replace("+00:00", "Z")
    ws_id = await seed_monitoring_workspace(factory, pr_number=194, head_sha="head-a")
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # git fetch origin development
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(
            head_sha="head-a",
            created_at="2026-05-06T10:00:00Z",
            committed_date="2026-05-06T10:00:00Z",
            threads=[
                {
                    "id": "T_resolved_late_greptile",
                    "isResolved": True,
                    "isOutdated": False,
                    "path": "src/awf/runtime/pr_monitor_runner/merge_loop.py",
                    "line": 430,
                    "comments": {
                        "nodes": [
                            {
                                "databaseId": 19401,
                                "bodyText": "resolved reviewer ping",
                                "author": {"login": "greptile-apps"},
                                "viewerDidAuthor": False,
                                "createdAt": late_anchor_text,
                                "updatedAt": late_anchor_text,
                            }
                        ]
                    },
                }
            ],
        ),
    )
    cmd.queue_result(returncode=0)  # gh pr merge if the refreshed anchor is missed
    cmd.queue_result(returncode=0, stdout="merge-sha\n")
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        pre_merge_settle_seconds=5,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=900,
        non_check_reviewer_logins=("greptile-apps",),
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=ws_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=194,
        status=_ready_status(
            pr_number=194,
            head_sha="head-a",
            quiet_period_anchor_at=old_anchor,
            quiet_period_anchor_source="review_thread_comment",
            latest_external_review_activity_at=old_anchor,
            latest_external_review_activity_source="review_thread_comment",
        ),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [5, 60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    started_prefix = "__awf_non_check_reviewer_settle_started__:194:head-a:"
    assert any(
        key.startswith(started_prefix) and value == "activity_wait"
        for key, value in state.threads_addressed_ids.items()
    )
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=ws_id)
    settle_operation = next(
        op for op in operations if op.payload["action"] == "reviewer_settle_wait"
    )
    assert settle_operation.payload["reason_code"] == "NON_CHECK_REVIEWER_SETTLE"
    assert settle_operation.payload["activity_anchor_at"] == late_anchor.isoformat()
    assert settle_operation.payload["activity_anchor_source"] == "review_thread_comment"


@pytest.mark.unit
async def test_manual_ready_handoff_waits_for_activity_quiet_window_before_notification(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    anchor = datetime.now(UTC)
    ws_id = await seed_monitoring_workspace(
        factory,
        pr_number=193,
        head_sha="head-a",
        auto_merge=False,
    )
    sleep_fn = RecordedSleep()
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=900,
        non_check_reviewer_logins=("greptile-apps",),
    )

    terminal = await runner._execute(
        action=NotifyHuman(),
        workspace_id=ws_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=193,
        status=_ready_status(
            pr_number=193,
            head_sha="head-a",
            quiet_period_anchor_at=anchor,
            quiet_period_anchor_source="review",
            latest_external_review_activity_at=anchor,
            latest_external_review_activity_source="review",
        ),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=ws_id)
    assert operations[0].payload["action"] == "reviewer_settle_wait"
    assert operations[0].payload["requested_action"] == "notify_human"
