"""Focused branch-coverage tests for PR monitor runner edge behavior (split part)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import (
    BITBUCKET_API_ERROR,
    BitbucketClientError,
)
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, RepoRef
from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.gates import (
    _handle_merge_gate_blocker,
    _MergeGateResult,
)
from awf.runtime.pr_monitor_runner.types import (
    BaseBehindCountError,
    BaseFetchError,
)
from awf.service.merge_queue import MergeQueueBlocker
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _status_for_helpers(
    *,
    head_sha: str = "abc1234567890def",
    threads: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    blocking_reviews: tuple[ReviewComment, ...] | None = None,
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=reviews,
        blocking_reviews=(
            tuple(review for review in reviews if review.blocks_merge)
            if blocking_reviews is None
            else blocking_reviews
        ),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


class _QueueAfterLockRunner(PullRequestMonitorRunner):
    def __init__(self, *, blocker: MergeQueueBlocker, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._blocker = blocker
        self.blocker_calls = 0

    async def _merge_queue_blockers_for_workspace(
        self,
        workspace_id: str,
    ) -> list[MergeQueueBlocker]:
        assert workspace_id
        self.blocker_calls += 1
        return [] if self.blocker_calls == 1 else [self._blocker]


def _retry_events(ws: Workspace) -> list:
    return [
        event
        for event in ws.events
        if event.event_type == "monitor.github_transient_error_retrying"
    ]


def _bitbucket_retry_events(ws: Workspace) -> list:
    return [
        event
        for event in ws.events
        if event.event_type == "monitor.bitbucket_transient_error_retrying"
    ]


async def _mark_refactor_task(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    auto_merge: bool,
) -> None:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.task_class = TaskClass.refactor_task.value
        ws.auto_merge = auto_merge
        await s.commit()


async def _seed_running_operation(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> str:
    async with factory() as s:
        operation = await OperationRepository(s).create(
            workspace_id=workspace_id,
            operation_type=OperationType.refresh,
            status=OperationStatus.running,
            payload={"source": "test", "keep": True},
            idempotency_key=f"op:{workspace_id}",
        )
        await s.commit()
        return operation.id


async def _update_workspace(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    **values: object,
) -> None:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        for key, value in values.items():
            setattr(ws, key, value)
        await s.commit()


async def _force_workspace_status(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    status: WorkspaceStatus,
) -> None:
    async with factory() as s:
        await s.execute(
            sa_update(Workspace).where(Workspace.id == workspace_id).values(status=status.value)
        )
        await s.commit()


@pytest.mark.unit
async def test_stale_manual_merge_dispatches_validation_before_handoff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=False)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload())

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value
        assert ws.failure_reason is None
        assert ws.failure_message is None
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
    validate_operations = [op for op in operations if op.type == OperationType.validate.value]
    assert len(validate_operations) == 1
    assert validate_operations[0].payload["recovery_mode"] == "validate_only"
    assert validate_operations[0].payload["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"


@pytest.mark.unit
async def test_stale_auto_merge_dispatches_validation_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload())

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
    assert [(op.type, op.payload) for op in operations] == [
        (
            OperationType.validate.value,
            {
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "validate_only",
                "requested_action": "validate",
                "reason": "Required validation tier has not passed for this merge candidate.",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "stale_reason": "validation_insufficient_tier",
                "recovery_mode": "validate_only",
                "pr_number": 42,
                "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
                "source_head_sha": "abc1234567890def",
                "source_base_sha": "a" * 40,
                "target_branch": "development",
                "remote_branch": f"awf/{workspace_id}",
            },
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "final_status",
    [
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroying,
        WorkspaceStatus.destroyed,
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
    ],
)
async def test_stale_recovery_dispatch_ignores_terminal_workspace_race(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    final_status: WorkspaceStatus,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload())

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    original_gate = runner._merge_gate_for_workspace

    async def _gate_then_terminal(
        workspace_id_arg: str,
        *,
        check_policy: bool = False,
    ) -> object:
        gate = await original_gate(workspace_id_arg, check_policy=check_policy)
        await _force_workspace_status(factory, workspace_id_arg, final_status)
        return gate

    runner._merge_gate_for_workspace = _gate_then_terminal  # type: ignore[method-assign]

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        ignored_events = [
            event for event in ws.events if event.event_type == "workspace.stale_callback_ignored"
        ]

    assert ws.status == final_status.value
    assert operations == []
    assert ignored_events[-1].reason_code == "STALE_CALLBACK_IGNORED"
    assert ignored_events[-1].payload == {
        "callback_source": "pr_monitor",
        "callback_action": "recovery_dispatch",
        "expected_status": WorkspaceStatus.monitoring_pr.value,
        "actual_status": final_status.value,
        "requested_status": WorkspaceStatus.ready.value,
        "reason_code": "STALE_CALLBACK_IGNORED",
    }


@pytest.mark.unit
async def test_stale_gate_handler_records_ignored_callback_for_terminal_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _force_workspace_status(factory, workspace_id, WorkspaceStatus.completed)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None

    handled = await _handle_merge_gate_blocker(
        runner,
        gate=_MergeGateResult(
            workspace=ws,
            stale_reason="validation_missing_for_current_head",
            req_action="validate",
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
        skip_initial_review_grace=True,
    )

    async with factory() as s:
        refreshed = await WorkspaceRepository(s).get(workspace_id)
        assert refreshed is not None
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        ignored_events = [
            event
            for event in refreshed.events
            if event.event_type == "workspace.stale_callback_ignored"
        ]

    assert handled is True
    assert refreshed.status == WorkspaceStatus.completed.value
    assert operations == []
    assert ignored_events[-1].payload == {
        "callback_source": "pr_monitor",
        "callback_action": "recovery_dispatch",
        "expected_status": WorkspaceStatus.monitoring_pr.value,
        "actual_status": WorkspaceStatus.completed.value,
        "requested_status": WorkspaceStatus.ready.value,
        "reason_code": "STALE_CALLBACK_IGNORED",
    }


@pytest.mark.unit
async def test_stale_recovery_dispatch_ignores_legacy_invalid_workspace_status(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload())

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    original_gate = runner._merge_gate_for_workspace

    async def _gate_then_legacy_status(
        workspace_id_arg: str,
        *,
        check_policy: bool = False,
    ) -> object:
        gate = await original_gate(workspace_id_arg, check_policy=check_policy)
        await _update_workspace(
            factory,
            workspace_id_arg,
            status="legacy-invalid-status",
        )
        return gate

    runner._merge_gate_for_workspace = _gate_then_legacy_status  # type: ignore[method-assign]

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        ignored_events = [
            event for event in ws.events if event.event_type == "workspace.stale_callback_ignored"
        ]

    assert ws.status == "legacy-invalid-status"
    assert operations == []
    assert ignored_events[-1].reason_code == "STALE_CALLBACK_IGNORED"
    assert ignored_events[-1].payload == {
        "callback_source": "pr_monitor",
        "callback_action": "recovery_dispatch",
        "expected_status": WorkspaceStatus.monitoring_pr.value,
        "actual_status": "legacy-invalid-status",
        "requested_status": WorkspaceStatus.ready.value,
        "reason_code": "STALE_CALLBACK_IGNORED",
    }


@pytest.mark.unit
async def test_pre_merge_recheck_github_error_fails_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=1, stderr="secondary fetch failed")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        pre_merge_settle_seconds=2,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert sleep_fn.calls == [2]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "pre-merge recheck" in (ws.failure_message or "")
        # The GitHub fault preserves ``GITHUB_API_ERROR`` end-to-end, matching the
        # ``fetch_pr_status``/``_execute`` termination paths rather than decaying to
        # the ``MONITOR_ABORT`` default.
        assert ws.events[-1].reason_code == "GITHUB_API_ERROR"


@pytest.mark.unit
async def test_pre_merge_recheck_base_fetch_error_fails_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        pre_merge_settle_seconds=2,
    )
    mocker.patch.object(
        runner,
        "_fetch_status_for_decision",
        mocker.AsyncMock(side_effect=BaseFetchError("base fetch died")),
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert sleep_fn.calls == [2]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "could not refresh base branch" in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "GIT_FETCH_BASE_FAILED"


@pytest.mark.unit
async def test_pre_merge_recheck_base_behind_error_fails_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        pre_merge_settle_seconds=2,
    )
    mocker.patch.object(
        runner,
        "_fetch_status_for_decision",
        mocker.AsyncMock(side_effect=BaseBehindCountError("rev-list died")),
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert sleep_fn.calls == [2]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "could not calculate base-behind count" in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "GIT_BASE_BEHIND_FAILED"


@pytest.mark.unit
async def test_pre_merge_recheck_transient_github_error_retries_later(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    operation_id = await _seed_running_operation(factory, workspace_id)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=1, stderr="HTTP 503 Service Unavailable")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        pre_merge_settle_seconds=2,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [2, 5]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.failure_message is None
        operation = await OperationRepository(s).get(operation_id)
        assert operation is not None
        assert operation.status == OperationStatus.running.value
        assert operation.payload == {"source": "test", "keep": True}
        events = _retry_events(ws)
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].payload["context"] == "pre_merge_recheck"
        assert events[0].payload["operation"] == "gh api graphql"
        assert events[0].payload["wait_seconds"] == 5


@pytest.mark.unit
async def test_pre_merge_recheck_transient_bitbucket_error_retries_later(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """A transient Bitbucket fault during the pre-merge recheck must wait and keep
    polling, symmetric to the GitHub arm — not escape uncaught or terminate."""
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        pre_merge_settle_seconds=2,
    )
    mocker.patch.object(
        runner,
        "_fetch_status_for_decision",
        mocker.AsyncMock(
            side_effect=BitbucketClientError(
                operation="bitbucket fetch_pr_status",
                status=503,
                body="service unavailable",
                reason_code=BITBUCKET_API_ERROR,
            )
        ),
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [2, 5]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.failure_message is None
        retry_events = _bitbucket_retry_events(ws)
        assert len(retry_events) == 1
        assert retry_events[0].reason_code == "BITBUCKET_TRANSIENT_RETRY"
        assert retry_events[0].payload["context"] == "pre_merge_recheck"
        assert retry_events[0].payload["wait_seconds"] == 5


@pytest.mark.unit
async def test_pre_merge_recheck_bitbucket_error_fails_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """A deterministic Bitbucket fault during the pre-merge recheck terminates the
    workspace with the actionable reason code preserved end-to-end.

    A non-auth 4xx (``BITBUCKET_API_ERROR`` 404) is genuinely deterministic —
    401/403 are now bounded-retryable (#515), so they route through the transient
    arm rather than terminating immediately."""
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        pre_merge_settle_seconds=2,
    )
    mocker.patch.object(
        runner,
        "_fetch_status_for_decision",
        mocker.AsyncMock(
            side_effect=BitbucketClientError(
                operation="bitbucket fetch_pr_status",
                status=404,
                body="not found",
                reason_code=BITBUCKET_API_ERROR,
            )
        ),
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert sleep_fn.calls == [2]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "bitbucket error during pre-merge recheck" in (ws.failure_message or "")
        assert ws.events[-1].reason_code == BITBUCKET_API_ERROR
        assert _bitbucket_retry_events(ws) == []


@pytest.mark.unit
async def test_pre_merge_recheck_unknown_status_after_retry_never_uses_old_green_snapshot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=1, stderr="HTTP 503 Service Unavailable")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload(check_state="PENDING"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        pre_merge_settle_seconds=2,
    )
    state = MonitorState()
    repo = RepoRef(owner="dimileeh", name="aira-web")
    status = _status_for_helpers()

    first_terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=repo,
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )
    second_terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=repo,
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert first_terminal is False
    assert second_terminal is False
    assert sleep_fn.calls == [2, 5, 2, 60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        events = _retry_events(ws)
        assert len(events) == 1
        assert events[0].payload["context"] == "pre_merge_recheck"


@pytest.mark.unit
async def test_pre_merge_recheck_dispatches_refreshed_non_merge_action(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload(check_state="PENDING"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        pre_merge_settle_seconds=2,
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [2, 60]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_merge_rejection_posts_human_notification_and_keeps_monitoring(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=1, stderr="protected branch requires approval")
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert any(
        key.startswith("__awf_notify__:abc1234567890def:") for key in state.threads_addressed_ids
    )
    assert cmd.calls[0].args[:4] == ["gh", "pr", "merge", "42"]
    assert cmd.calls[1].args[:4] == ["gh", "pr", "comment", "42"]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert _retry_events(ws) == []


@pytest.mark.unit
async def test_merge_queue_wait_records_event_once_per_head_and_blocker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    blocker = MergeQueueBlocker(
        candidate_id="mc_older",
        workspace_id="ws_older",
        attempt_id="attempt_older",
        task_id="task_older",
        title="Older candidate",
        pr_url="https://github.com/dimileeh/aira-web/pull/41",
        pr_number=41,
        status="open",
        blocker_state="ready",
    )
    state = MonitorState()
    status = _status_for_helpers()

    await runner._wait_for_merge_queue(
        blockers=[blocker],
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
        pr_number=42,
        status=status,
        state=state,
        monitor_log=None,
    )
    await runner._wait_for_merge_queue(
        blockers=[blocker],
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
        pr_number=42,
        status=status,
        state=state,
        monitor_log=None,
    )

    assert sleep_fn.calls == [60, 60]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        events = [
            event for event in ws.events if event.event_type == "workspace.merge_queue_waiting"
        ]
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        assert len(events) == 1
        assert events[0].payload["blocker_candidate_id"] == "mc_older"
        assert [(op.type, op.status, op.payload["action"]) for op in operations] == [
            (
                OperationType.monitor_state.value,
                OperationStatus.succeeded.value,
                "merge_queue_wait",
            )
        ]
        assert operations[0].payload["reason_code"] == "MERGE_QUEUE_WAIT"
        assert operations[0].payload["blocker_candidate_id"] == "mc_older"


@pytest.mark.unit
async def test_monitor_state_sleep_failure_marks_operation_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    class FailingSleep(RecordedSleep):
        async def __call__(self, seconds: float) -> None:
            assert seconds == 15
            raise RuntimeError("sleep backend unavailable")

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=FailingSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(RuntimeError, match="sleep backend unavailable"):
        await runner._sleep_with_monitor_state_operation(
            workspace_id=workspace_id,
            action="grace_wait",
            requested_action="validate",
            reason="Initial review grace period is still active.",
            reason_code="INITIAL_REVIEW_GRACE",
            pr_number=42,
            status=_status_for_helpers(),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            wait_seconds=15,
        )

    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        assert len(operations) == 1
        operation = operations[0]
        assert operation.type == OperationType.monitor_state.value
        assert operation.status == OperationStatus.failed.value
        assert operation.error_code == "INITIAL_REVIEW_GRACE"
        assert operation.error_message == "sleep backend unavailable"
        assert operation.result == {
            "status": "failed",
            "outcome": "wait_failed",
            "reason_code": "INITIAL_REVIEW_GRACE",
        }


@pytest.mark.unit
async def test_merge_queue_blocker_after_lock_defers_merge_without_calling_github_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    blocker = MergeQueueBlocker(
        candidate_id="mc_after_lock",
        workspace_id="ws_older",
        attempt_id="attempt_older",
        task_id="task_older",
        title="Older candidate",
        pr_url="https://github.com/dimileeh/aira-web/pull/41",
        pr_number=41,
        status="open",
        blocker_state="ready",
    )
    cmd = FakeCommandRunner()
    runner = _QueueAfterLockRunner(
        blocker=blocker,
        session_factory=factory,
        runner=cmd,
        adapter=FakeAdapter(),
        gh=GitHubClient(cmd),
        monitor_config=MonitorConfig(
            auto_merge=True,
            poll_interval_seconds=60,
            initial_review_grace_period_seconds=0,
            pre_merge_settle_seconds=0,
            non_check_reviewer_settle_seconds=0,
        ),
        runner_config=MonitorRunnerConfig(max_outer_iterations=3, max_fix_cycle_passes=3),
        sleep=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert runner.blocker_calls == 2
    assert sleep_fn.calls == [60]
    assert cmd.calls == []
