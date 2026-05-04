"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_mock
import structlog
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef
from awf.db.base import Base
from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import ValidationRun, Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventCreate,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_engine, make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
    CheckFailure,
    CheckState,
    CheckTiming,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReportCiFailure,
    ReviewComment,
    ReviewThread,
    SyncBase,
)
from awf.runtime.pr_monitor_runner import (
    BaseBehindCountError,
    BaseFetchError,
    MonitorRunnerConfig,
    ProviderRecoveryRetryError,
    PullRequestMonitorRunner,
    _as_utc,
    _candidate_stale_required_action,
    _changed_paths_from_porcelain,
    _collect_defer_items,
    _GitPushResult,
    _has_successful_validation_for_pr_head,
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _initial_review_grace_state_for_persistence,
    _initial_review_grace_state_for_runtime,
    _initial_review_grace_wait_seconds,
    _initial_review_grace_wall_started_value_from_datetime,
    _is_pending_check,
    _is_transient_github_client_error,
    _merge_rejection_reason,
    _non_check_reviewer_settle_started_key,
    _non_check_reviewer_settle_state_for_persistence,
    _non_check_reviewer_settle_state_for_runtime,
    _NonCheckReviewerSettleDecision,
    _notify_human_reason,
    _redact_and_truncate_github_error,
    _stale_pending_check_warnings,
    _target_reconcile_failure_payload,
    _target_reconcile_payload,
    _with_ci_failures,
)
from awf.service.alembic_resolver import AlembicResolveResult, AlembicResolveStatus
from awf.service.merge_queue import MergeQueueBlocker
from awf.service.target_branch_monitor import (
    TargetBranchMonitorError,
    TargetBranchMonitorResult,
    TargetBranchMonitorStatus,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    review_node,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _status_for_helpers(
    *,
    threads: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=reviews,
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


class _FailingLogSink:
    async def write(self, data: str) -> None:
        del data
        raise RuntimeError("log sink unavailable")


class _RecordingLogSink:
    stream_id = "monitor.log"

    def __init__(self) -> None:
        self.lines: list[str] = []

    async def write(self, data: str) -> None:
        self.lines.append(data)


class _ExplodingRunner:
    async def run(self, args: list[str], **_kwargs: object) -> object:
        del args
        raise RuntimeError("runner unavailable")


class _CleanupFailingAdapter(FakeAdapter):
    async def run(self, **_kwargs: object) -> object:  # type: ignore[override]
        raise ComposeExecCleanupError(
            invocation_id="awf_monitor_cleanup_failed",
            source="agent",
            label="monitor",
            message="tagged process still alive",
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


class _StopAfterRetryError(RuntimeError):
    pass


class _StopAfterRetrySleep(RecordedSleep):
    async def __call__(self, seconds: float) -> None:
        await super().__call__(seconds)
        raise _StopAfterRetryError


def _retry_events(ws: Workspace) -> list:
    return [
        event
        for event in ws.events
        if event.event_type == "monitor.github_transient_error_retrying"
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
            sa_update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(status=status.value)
        )
        await s.commit()


@pytest.mark.unit
async def test_monitor_run_fails_cleanly_when_pr_number_is_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    await _update_workspace(factory, workspace_id, pr_number=None)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
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
        assert ws.status == WorkspaceStatus.failed.value
        assert "without a pr_number" in (ws.failure_message or "")
    assert cmd.calls == []


@pytest.mark.unit
async def test_monitor_run_fails_cleanly_when_sync_workspace_has_no_remote_push_branch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    await _update_workspace(
        factory,
        workspace_id,
        task_kind="sync_feature_pr",
        branch_name="feature-sync/local-only",
        remote_push_branch=None,
    )
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload())
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
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
        assert ws.status == WorkspaceStatus.failed.value
        assert "no remote_push_branch" in (ws.failure_message or "")
        assert "sync_feature_pr" in (ws.failure_message or "")
    assert [call.args[0:3] for call in cmd.calls] == [
        ["git", "-C", str(tmp_path / "worktrees" / workspace_id)],
        ["git", "-C", str(tmp_path / "worktrees" / workspace_id)],
        ["gh", "api", "graphql"],
    ]


@pytest.mark.unit
async def test_monitor_run_terminates_on_github_status_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=1, stderr="gh auth failed")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
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
        assert ws.status == WorkspaceStatus.failed.value
        assert "github error" in (ws.failure_message or "")
        assert "gh auth failed" in (ws.failure_message or "")
        assert _retry_events(ws) == []


@pytest.mark.unit
async def test_monitor_run_transient_status_fetch_preserves_state_operations_and_lifecycle(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = _StopAfterRetrySleep()
    workspace_id = await seed_monitoring_workspace(factory)
    operation_id = await _seed_running_operation(factory, workspace_id)
    started_at = datetime(2026, 1, 2, tzinfo=UTC)
    await _update_workspace(
        factory,
        workspace_id,
        monitor_iter_count=7,
        monitor_threads_addressed={"T_old": "defer"},
        monitor_last_commit_sha="oldsha",
        monitor_started_at=started_at,
    )
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(
        returncode=1,
        stderr="HTTP 502 Bad Gateway for token ghp_statusretrysecret",
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(_StopAfterRetryError):
        await runner.run(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    assert sleep_fn.calls == [60]
    assert [call.args[:3] for call in cmd.calls] == [
        ["git", "-C", str(tmp_path / "worktrees" / workspace_id)],
        ["git", "-C", str(tmp_path / "worktrees" / workspace_id)],
        ["gh", "api", "graphql"],
    ]
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.failure_message is None
        assert ws.monitor_iter_count == 7
        assert ws.monitor_threads_addressed == {"T_old": "defer"}
        assert ws.monitor_last_commit_sha == "oldsha"
        assert _as_utc(ws.monitor_started_at) == started_at
        operation = await OperationRepository(s).get(operation_id)
        assert operation is not None
        assert operation.status == OperationStatus.running.value
        assert operation.payload == {"source": "test", "keep": True}
        events = _retry_events(ws)
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].old_state == WorkspaceStatus.monitoring_pr.value
        assert events[0].new_state == WorkspaceStatus.monitoring_pr.value
        assert events[0].payload == {
            "context": "fetch_pr_status",
            "operation": "gh api graphql",
            "returncode": 1,
            "pr_number": 42,
            "wait_seconds": 60,
            "message": (
                "gh api graphql failed (exit=1): HTTP 502 Bad Gateway for token <redacted>"
            ),
            "stderr": "HTTP 502 Bad Gateway for token <redacted>",
        }


@pytest.mark.unit
async def test_monitor_run_retries_transient_github_status_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert sleep_fn.calls == [60]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value


@pytest.mark.unit
def test_transient_github_error_classifier_keeps_auth_errors_terminal() -> None:
    assert _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="HTTP 503 Service Unavailable",
        )
    )
    assert _is_transient_github_client_error(
        GitHubClientError(
            operation="gh pr merge",
            returncode=1,
            stderr="secondary rate limit hit; please try again",
        )
    )
    assert not _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="Bad credentials",
        )
    )
    assert not _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="review is required before merging",
        )
    )


@pytest.mark.unit
def test_github_error_redaction_covers_app_jwt_and_bearer_tokens() -> None:
    app_token = "gha_11AA22BB33CC44DD"
    jwt_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123"
    bearer_token = "opaqueBearerToken123"
    redacted = _redact_and_truncate_github_error(
        "HTTP 503 "
        f"{app_token} "
        f"jwt={jwt_token} "
        f"Authorization: Bearer {bearer_token}"
    )

    assert app_token not in redacted
    assert jwt_token not in redacted
    assert bearer_token not in redacted
    assert redacted.count("<redacted>") == 3
    assert "Authorization: Bearer <redacted>" in redacted


@pytest.mark.unit
async def test_transient_retry_event_payload_is_structured_and_redacted(
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
    secret = "github_pat_11AA22BB33CC44DD"
    noisy_stderr = (
        f"HTTP 503 Service Unavailable for {secret} at "
        f"https://user:{secret}@github.com/example/repo " + ("x" * 600)
    )

    retried = await runner._wait_after_transient_github_error(
        GitHubClientError(operation="gh api graphql", returncode=1, stderr=noisy_stderr),
        workspace_id=workspace_id,
        pr_number=42,
        context="fetch_pr_status",
        monitor_log=None,
    )

    assert retried is True
    assert sleep_fn.calls == [60]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        events = _retry_events(ws)
        assert len(events) == 1
        event = events[0]
        assert event.reason_code == "GITHUB_TRANSIENT_RETRY"
        payload = event.payload
        assert payload is not None
        assert payload["context"] == "fetch_pr_status"
        assert payload["operation"] == "gh api graphql"
        assert payload["returncode"] == 1
        assert payload["pr_number"] == 42
        assert payload["wait_seconds"] == 60
        assert secret not in str(payload)
        assert "https://<redacted>@github.com/example/repo" in payload["stderr"]
        assert len(payload["stderr"]) <= 400
        assert len(payload["message"]) <= 400


@pytest.mark.unit
async def test_stale_merge_without_auto_merge_aborts_workspace(
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
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_message == "monitor: abort (stale)"


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
            event
            for event in ws.events
            if event.event_type == "workspace.stale_callback_ignored"
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
            event
            for event in ws.events
            if event.event_type == "workspace.stale_callback_ignored"
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
    assert sleep_fn.calls == [2, 60]
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
        assert events[0].payload["wait_seconds"] == 60


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
    assert sleep_fn.calls == [2, 60, 2, 60]
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
        key.startswith("__awf_notify__:abc1234567890def:")
        for key in state.threads_addressed_ids
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


@pytest.mark.unit
async def test_transient_github_merge_error_retries_without_human_escalation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=1, stderr="HTTP 504 Gateway Timeout")
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
    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:3] == ["gh", "pr", "merge"]
    assert state.threads_addressed_ids == {}
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.failure_message is None
        events = _retry_events(ws)
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].payload["context"] == "merge_pr"
        assert events[0].payload["operation"] == "gh pr merge"
        assert events[0].payload["wait_seconds"] == 60


@pytest.mark.unit
async def test_non_transient_github_merge_error_records_failed_audit_and_redacts(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    secret_stderr = (
        "Resource not accessible by integration for "
        "https://user:raw_secret_value@github.com/org/repo "
        "Authorization: Bearer opaqueBearerToken123"
    )
    cmd.queue_result(returncode=1, stderr=secret_stderr)
    cmd.queue_result(returncode=0)  # gh pr comment fallback
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()
    monitor_log = _RecordingLogSink()

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
        monitor_log=monitor_log,  # type: ignore[arg-type]
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert [call.args[:3] for call in cmd.calls] == [
        ["gh", "pr", "merge"],
        ["gh", "pr", "comment"],
    ]
    async with factory() as s:
        operations = await OperationRepository(s).list_all(
            workspace_id=workspace_id,
            limit=20,
        )
        attempt_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.merge_attempt",
            limit=10,
        )
        result_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.merge_result",
            limit=10,
        )

    merge_operation = next(
        operation
        for operation in operations
        if operation.type == "monitor_state"
        and isinstance(operation.payload, dict)
        and operation.payload.get("action") == "merge"
    )
    assert merge_operation.status == OperationStatus.failed.value
    assert merge_operation.error_code == "GITHUB_MERGE_FAILED"
    assert merge_operation.error_message is not None
    assert "raw_secret_value" not in merge_operation.error_message
    assert "opaqueBearerToken123" not in merge_operation.error_message
    assert "https://[redacted]@github.com/org/repo" in merge_operation.error_message

    assert len(attempt_events) == 1
    assert attempt_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "pr_monitor",
        "source": "pr_monitor",
        "action": "merge",
        "outcome": "attempted",
        "reason_code": "MERGE",
        "operation_id": merge_operation.id,
        "operation_type": "monitor_state",
        "pr_number": 42,
        "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
        "source_head_sha": "abc1234567890def",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
        "branch_name": f"awf/{workspace_id}",
        "evidence": {"log_stream_refs": {"monitor": "monitor.log"}},
    }
    assert len(result_events) == 1
    assert result_events[0].reason_code == "GITHUB_MERGE_FAILED"
    assert result_events[0].payload is not None
    assert result_events[0].payload["outcome"] == "failed"
    assert result_events[0].payload["operation_id"] == merge_operation.id
    assert result_events[0].payload["pr_number"] == 42
    assert result_events[0].payload["pr_url"] == (
        "https://github.com/dimileeh/aira-web/pull/42"
    )
    assert result_events[0].payload["source_head_sha"] == "abc1234567890def"
    assert result_events[0].payload["target_branch"] == "development"
    assert result_events[0].payload["evidence"]["operation"] == "merge_pr"
    assert result_events[0].payload["evidence"]["log_stream_refs"] == {
        "monitor": "monitor.log"
    }
    assert "raw_secret_value" not in repr(result_events[0].payload)
    assert "opaqueBearerToken123" not in repr(result_events[0].payload)
    assert "https://[redacted]@github.com/org/repo" in repr(result_events[0].payload)


@pytest.mark.unit
async def test_transient_human_notification_comment_error_retries_without_crashing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=NotifyHuman(message="branch protection temporarily unavailable"),
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
    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:3] == ["gh", "pr", "comment"]
    assert state.threads_addressed_ids == {}
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.failure_message is None
        events = _retry_events(ws)
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].payload["context"] == "post_human_notification"
        assert events[0].payload["operation"] == "gh pr comment"
        assert events[0].payload["wait_seconds"] == 60


@pytest.mark.unit
async def test_non_transient_human_notification_comment_error_raises(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="bad credentials")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(GitHubClientError, match="bad credentials"):
        await runner._execute(
            action=NotifyHuman(message="manual review needed"),
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


@pytest.mark.unit
async def test_fix_cycle_treats_transient_settle_poll_as_retryable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    cmd.queue_result(returncode=0, stdout="{}")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_retry",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert sleep_fn.calls == [30, 60]
    assert state.threads_addressed_ids == {"T_retry": "fix_committed"}
    assert [call.args[:3] for call in cmd.calls] == [
        ["gh", "api", "graphql"],
        ["git", "-C", str(tmp_path / "worktrees" / workspace_id)],
        ["gh", "api", "graphql"],
    ]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        events = _retry_events(ws)
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].payload["context"] == "fix_cycle_settle_fetch_pr_status"
        assert events[0].payload["operation"] == "gh api graphql"


@pytest.mark.unit
async def test_resolve_thread_transient_failure_requeues_thread_safely(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="newsha\n")
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_resolve",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert sleep_fn.calls == [30, 60]
    assert "T_resolve" not in state.threads_addressed_ids
    assert state.last_push_sha == "newsha"
    assert [call.args[:3] for call in cmd.calls] == [
        ["gh", "api", "graphql"],
        ["git", "-C", str(tmp_path / "worktrees" / workspace_id)],
        ["git", "-C", str(tmp_path / "worktrees" / workspace_id)],
        ["gh", "api", "graphql"],
    ]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        events = _retry_events(ws)
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].payload["context"] == "resolve_thread"
        assert events[0].payload["operation"] == "gh api graphql"
    assert len(resolution_events) == 1
    assert resolution_events[0].payload is not None
    assert resolution_events[0].payload["action"] == "resolve_thread"
    assert resolution_events[0].payload["outcome"] == "requeued"
    assert resolution_events[0].payload["evidence"] == {
        "thread_ids": ["T_resolve"],
        "resolved_thread_count": 0,
        "requeued_thread_count": 1,
        "error_message": "gh api graphql failed (exit=1): HTTP 502 Bad Gateway",
    }
    assert "please adjust this" not in repr(resolution_events[0].payload)


@pytest.mark.unit
async def test_fix_cycle_reraises_non_transient_settle_poll_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=1, stderr="bad credentials")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_auth",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="reviewer",
    )

    with pytest.raises(GitHubClientError, match="bad credentials"):
        await runner._run_fix_cycle(
            workspace_id="ws_auth",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            initial_threads=(thread,),
            initial_reviews=(),
            state=MonitorState(),
            remote_branch="awf/ws_auth",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )


@pytest.mark.unit
async def test_fix_cycle_zero_passes_still_runs_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    object.__setattr__(runner._runner_config, "max_fix_cycle_passes", 0)

    await runner._run_fix_cycle(
        workspace_id="ws_zero_pass",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        initial_threads=(),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_zero_pass",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:2] == ["git", "-C"]


@pytest.mark.unit
async def test_best_effort_monitor_log_and_missing_workspace_event_append_do_not_raise(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._write_monitor_log(_FailingLogSink(), {"event": "monitor.test"})  # type: ignore[arg-type]
    await runner._append_workspace_events(
        workspace_id="ws_missing",
        events=[
            WorkspaceEventCreate(
                event_type="workspace.test",
                reason_code="TEST",
                payload={"ok": True},
            )
        ],
    )


@pytest.mark.unit
async def test_post_human_notification_dedup_skips_github_call(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    status = _status_for_helpers()
    state = MonitorState(
        threads_addressed_ids={"__awf_notify__:abc1234567890def:manual": "notified"}
    )

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=status,
        state=state,
        blocker_reason="manual",
    )

    assert cmd.calls == []


@pytest.mark.unit
async def test_defer_signal_write_failure_is_best_effort(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    artifact_file = tmp_path / "artifacts-file"
    artifact_file.write_text("not a directory", encoding="utf-8")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifact_file,
    )

    runner._write_defer_signal(
        workspace_id="ws_defer",
        pr_number=42,
        terminal_action="Abort",
        merged=False,
        status=_status_for_helpers(),
        state=MonitorState(),
    )

    assert artifact_file.read_text(encoding="utf-8") == "not a directory"


@pytest.mark.unit
async def test_target_branch_reconcile_failure_appends_workspace_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    async def failing_reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        assert repo_url == "git@github.com:dimileeh/aira-web.git"
        assert branch == "development"
        raise RuntimeError("target branch locked")

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=failing_reconciler,
    )

    with structlog.testing.capture_logs() as captured:
        await runner._reconcile_target_branch_after_merge(
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            base_branch="development",
        )

    failure_log = next(
        event
        for event in captured
        if event.get("event") == "monitor.target_branch_reconcile_failed"
    )
    assert failure_log["status"] == "failed"
    assert failure_log["reason_code"] == "TARGET_BRANCH_RECONCILE_FAILED"
    assert failure_log["error_type"] == "RuntimeError"
    assert failure_log["resolver_results"] == []
    assert failure_log["commit_sha"] is None
    assert failure_log["pushed"] is False
    assert failure_log["dry_run"] is None
    assert failure_log["commit_allowed"] is None

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.events[-1].event_type == "target_branch.reconcile_failed"
        assert ws.events[-1].reason_code == "TARGET_BRANCH_RECONCILE_FAILED"
        assert ws.events[-1].payload["error"] == "target branch locked"


@pytest.mark.unit
async def test_target_branch_reconcile_failure_reuses_exception_payload(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    class _CountingResultError(Exception):
        def __init__(self) -> None:
            super().__init__("target branch locked " + ("x" * 1200))
            self.result_accesses = 0
            self._result = CommandResult(
                returncode=128,
                stdout="stdout " + ("o" * 1200),
                stderr="stderr " + ("e" * 1200),
                reason_code="GIT_FAILED",
            )

        @property
        def result(self) -> CommandResult:
            self.result_accesses += 1
            return self._result

    failure = _CountingResultError()

    async def failing_reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        del repo_url, branch, workspace_id
        raise failure

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=failing_reconciler,
    )

    with structlog.testing.capture_logs() as captured:
        await runner._reconcile_target_branch_after_merge(
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            base_branch="development",
        )

    failure_log = next(
        event
        for event in captured
        if event.get("event") == "monitor.target_branch_reconcile_failed"
    )
    assert failure.result_accesses == 1
    assert len(failure_log["error"]) == 500
    assert len(failure_log["stderr"]) == 500
    assert len(failure_log["stdout"]) == 500

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        event_payload = ws.events[-1].payload
        assert len(event_payload["error"]) == 1000
        assert len(event_payload["stderr"]) == 1000
        assert len(event_payload["stdout"]) == 1000


@pytest.mark.unit
def test_target_reconcile_failure_payload_uses_command_error_contract() -> None:
    result = CommandResult(
        returncode=128,
        stdout="fatal stdout",
        stderr="fatal stderr",
        reason_code="GIT_FAILED",
    )
    exc = TargetBranchMonitorError(
        operation="target_branch.git_fetch",
        result=result,
    )
    exc.target_reconcile_payload = lambda: {  # type: ignore[attr-defined]
        "status": "committed",
        "resolver_results": [{"status": "resolved"}],
        "commit_sha": "abc123",
        "pushed": True,
    }

    payload = _target_reconcile_failure_payload(exc, error_limit=100)

    assert payload["status"] == "failed"
    assert "target_reconcile_status" not in payload
    assert payload["resolver_results"] == []
    assert payload["commit_sha"] is None
    assert payload["pushed"] is False
    assert payload["operation"] == "target_branch.git_fetch"
    assert payload["returncode"] == 128
    assert payload["command_reason_code"] == "GIT_FAILED"
    assert payload["stderr"] == "fatal stderr"
    assert payload["stdout"] == "fatal stdout"


@pytest.mark.unit
async def test_target_branch_reconcile_success_appends_payload_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    async def reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        return {
            "status": "TARGET_BRANCH_FAST_FORWARDED",
            "repo_url": repo_url,
            "branch": branch,
            "workspace_id": workspace_id,
        }

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=reconciler,
    )

    await runner._reconcile_target_branch_after_merge(
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.events[-1].event_type == "target_branch.reconciled"
        assert ws.events[-1].reason_code == "TARGET_BRANCH_FAST_FORWARDED"
        assert ws.events[-1].payload["branch"] == "development"


@pytest.mark.unit
async def test_target_branch_reconcile_event_preserves_resolver_operator_details(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    async def reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        checkout = tmp_path / "checkout"
        generated = checkout / "migrations" / "versions" / "merge001_merge_alembic_heads.py"
        return TargetBranchMonitorResult(
            repo_url=repo_url,
            branch=branch,
            checkout_path=checkout,
            status=TargetBranchMonitorStatus.committed,
            resolver_results=(
                AlembicResolveResult(
                    status=AlembicResolveStatus.resolved,
                    reason_code="ALEMBIC_HEADS_MERGED",
                    heads=("left001", "right001"),
                    generated_revision="merge001",
                    generated_path=generated,
                    generated_path_relative="migrations/versions/merge001_merge_alembic_heads.py",
                    message="Generated Alembic merge revision for 2 heads.",
                ),
            ),
            commit_sha="abc123",
            pushed=True,
            changed_paths=("migrations/versions/merge001_merge_alembic_heads.py",),
        )

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=reconciler,
    )

    with structlog.testing.capture_logs() as captured:
        await runner._reconcile_target_branch_after_merge(
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            base_branch="development",
        )

    reconciled_log = next(
        event
        for event in captured
        if event.get("event") == "monitor.target_branch_reconciled"
    )
    assert reconciled_log["status"] == "committed"
    assert reconciled_log["commit_sha"] == "abc123"
    assert reconciled_log["pushed"] is True
    assert reconciled_log["dry_run"] is False
    assert reconciled_log["commit_allowed"] is True
    assert reconciled_log["policy_reason_code"] is None
    assert reconciled_log["changed_paths"] == [
        "migrations/versions/merge001_merge_alembic_heads.py"
    ]
    logged_resolver = reconciled_log["resolver_results"][0]
    assert logged_resolver["reason_code"] == "ALEMBIC_HEADS_MERGED"
    assert logged_resolver["heads"] == ["left001", "right001"]
    assert logged_resolver["generated_revision"] == "merge001"
    assert logged_resolver["generated_path_relative"] == (
        "migrations/versions/merge001_merge_alembic_heads.py"
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        payload = ws.events[-1].payload
        assert payload["status"] == "committed"
        assert payload["commit_sha"] == "abc123"
        assert payload["pushed"] is True
        assert payload["branch"] == "development"
        assert payload["changed_paths"] == [
            "migrations/versions/merge001_merge_alembic_heads.py"
        ]
        resolver = payload["resolver_results"][0]
        assert resolver["reason_code"] == "ALEMBIC_HEADS_MERGED"
        assert resolver["heads"] == ["left001", "right001"]
        assert resolver["generated_revision"] == "merge001"
        assert resolver["generated_path_relative"] == (
            "migrations/versions/merge001_merge_alembic_heads.py"
        )


@pytest.mark.unit
async def test_completed_filesystem_gc_exception_is_logged_and_swallowed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    class _ExplodingSessionFactory:
        def __call__(self) -> object:
            raise RuntimeError("database unavailable")

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.session_factory = _ExplodingSessionFactory()  # type: ignore[assignment]

    with structlog.testing.capture_logs() as captured:
        await runner._gc_completed_workspace_filesystem("ws_gc")

    assert any(
        event.get("event") == "monitor.filesystem_gc_raised"
        and event.get("workspace_id") == "ws_gc"
        for event in captured
    )


@pytest.mark.unit
async def test_dirty_worktree_helper_returns_false_for_non_commit_cases(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    case_number = 0

    async def run_case(
        *,
        workspace_exists: bool = True,
        queued: list[tuple[int, str, str]],
    ) -> tuple[bool, list[list[str]]]:
        nonlocal case_number
        case_number += 1
        cmd = FakeCommandRunner()
        for returncode, stdout, stderr in queued:
            cmd.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=FakeAdapter(),
            sleep_fn=RecordedSleep(),
            worktrees_root=tmp_path / "worktrees",
        )
        workspace_id = f"ws_dirty_{case_number}"
        if workspace_exists:
            (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
        result = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: dirty",
        )
        return result, [call.args for call in cmd.calls]

    missing_result, missing_calls = await run_case(workspace_exists=False, queued=[])
    status_result, status_calls = await run_case(queued=[(1, "", "status failed")])
    clean_result, clean_calls = await run_case(queued=[(0, "", "")])
    add_result, add_calls = await run_case(queued=[(0, " M a.py\n", ""), (1, "", "add failed")])
    cached_result, cached_calls = await run_case(
        queued=[(0, " M a.py\n", ""), (0, "", ""), (0, "", "")]
    )
    commit_result, commit_calls = await run_case(
        queued=[(0, " M a.py\n", ""), (0, "", ""), (1, "", ""), (1, "", "commit failed")]
    )

    assert missing_result is False
    assert missing_calls == []
    assert status_result is False
    assert [args[-2:] for args in status_calls] == [["status", "--porcelain"]]
    assert clean_result is False
    assert [args[-2:] for args in clean_calls] == [["status", "--porcelain"]]
    assert add_result is False
    assert [args[-2:] for args in add_calls] == [["status", "--porcelain"], ["add", "-A"]]
    assert cached_result is False
    assert [args[-2:] for args in cached_calls[:2]] == [["status", "--porcelain"], ["add", "-A"]]
    assert cached_calls[2][-3:] == ["diff", "--cached", "--quiet"]
    assert commit_result is False
    assert commit_calls[-1][-3:] == ["commit", "-m", "fix: dirty"]


@pytest.mark.unit
async def test_execute_report_ci_failure_dispatches_fix_and_increments_iteration(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(returncode=1, stdout="partial CI fix")
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=ReportCiFailure(
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="traceback"),)
        ),
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
    assert state.iter_count == 1
    assert "traceback" in adapter.calls[0]
    assert cmd.calls[-1].args[-2:] == ["origin", f"HEAD:refs/heads/awf/{workspace_id}"]
    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
    ci_operation = next(operation for operation in operations if operation.type == "ci_repair")
    assert len(push_events) == 1
    assert push_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "pr_monitor",
        "source": "pr_monitor",
        "action": "ci_repair_push",
        "outcome": "succeeded",
        "reason_code": "CI_REPAIR",
        "operation_id": ci_operation.id,
        "operation_type": "ci_repair",
        "pr_number": 42,
        "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
        "source_head_sha": "abc1234567890def",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
        "branch_name": f"awf/{workspace_id}",
    }


@pytest.mark.unit
async def test_execute_report_ci_failure_push_failure_records_failed_audit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="partial CI fix")
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(
        returncode=128,
        stderr=(
            "fatal: unable to access "
            "https://user:ghp_should_not_persist@github.com/org/repo"
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=ReportCiFailure(
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="traceback"),)
        ),
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
    assert state.iter_count == 1
    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
    ci_operation = next(operation for operation in operations if operation.type == "ci_repair")
    assert ci_operation.status == OperationStatus.failed.value
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GIT_PUSH_FAILED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "ci_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["operation_id"] == ci_operation.id
    assert push_events[0].payload["operation_type"] == "ci_repair"
    assert push_events[0].payload["evidence"]["operation"] == "git push"
    assert push_events[0].payload["evidence"]["returncode"] == 128
    assert "ghp_should_not_persist" not in repr(push_events[0].payload)
    assert "https://[redacted]@github.com/org/repo" in repr(push_events[0].payload)


@pytest.mark.unit
async def test_monitor_adapter_cleanup_failure_terminates_without_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=_CleanupFailingAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    terminal = await runner._execute(
        action=ReportCiFailure(
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="traceback"),)
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
    )

    assert terminal is True
    assert cmd.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "EXEC_PROCESS_CLEANUP_FAILED" in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"


@pytest.mark.unit
async def test_monitor_comment_cleanup_failure_terminates_without_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    thread = ReviewThread(
        thread_id="T1",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=_CleanupFailingAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    terminal = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(threads=(thread,)),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert cmd.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.events[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"


@pytest.mark.unit
async def test_monitor_comment_repair_push_failure_records_failed_audit_and_requeues(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed locally")
    workspace_id = await seed_monitoring_workspace(factory)
    thread = ReviewThread(
        thread_id="T_push",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(
        returncode=128,
        stderr=(
            "fatal: unable to access "
            "https://user:ghp_should_not_persist@github.com/org/repo"
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(threads=(thread,)),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.iter_count == 1
    assert "T_push" not in state.threads_addressed_ids
    assert sum(call.args[:3] == ["gh", "api", "graphql"] for call in cmd.calls) == 1
    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
    comment_operation = next(
        operation for operation in operations if operation.type == "comment_repair"
    )
    assert comment_operation.status == OperationStatus.failed.value
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GIT_PUSH_FAILED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "comment_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["operation_id"] == comment_operation.id
    assert push_events[0].payload["operation_type"] == "comment_repair"
    assert push_events[0].payload["evidence"]["operation"] == "git push"
    assert push_events[0].payload["evidence"]["returncode"] == 128
    assert "ghp_should_not_persist" not in repr(push_events[0].payload)
    assert "https://[redacted]@github.com/org/repo" in repr(push_events[0].payload)
    assert resolution_events == []


@pytest.mark.unit
async def test_monitor_sync_base_cleanup_failure_terminates_without_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # fetch
    cmd.queue_result(returncode=1, stderr="conflict")  # merge
    cmd.queue_result(returncode=0, stdout="UU src/app.py\n")  # status
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=_CleanupFailingAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    terminal = await runner._execute(
        action=SyncBase(),
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
    assert [call.args[-2:] for call in cmd.calls] == [
        ["merge", "--abort"],
        ["origin", "+refs/heads/development:refs/remotes/origin/development"],
        ["--no-edit", "origin/development"],
        ["status", "--porcelain"],
    ]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.events[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"


@pytest.mark.unit
async def test_execute_sync_base_records_branch_push_audit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # fetch
    cmd.queue_result(returncode=0)  # merge
    cmd.queue_result(returncode=0)  # push
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=SyncBase(),
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
    assert state.iter_count == 1
    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
    sync_operation = next(operation for operation in operations if operation.type == "sync_base")
    assert len(push_events) == 1
    assert push_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "pr_monitor",
        "source": "pr_monitor",
        "action": "sync_base_push",
        "outcome": "succeeded",
        "reason_code": "SYNC_BASE",
        "operation_id": sync_operation.id,
        "operation_type": "sync_base",
        "pr_number": 42,
        "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
        "source_head_sha": "abc1234567890def",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
        "branch_name": f"awf/{workspace_id}",
    }


@pytest.mark.unit
async def test_execute_sync_base_push_failure_records_failed_audit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # fetch
    cmd.queue_result(returncode=0)  # merge
    cmd.queue_result(
        returncode=128,
        stderr="remote: invalid token ghp_should_not_persist",
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=SyncBase(),
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
    assert state.iter_count == 1
    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
    sync_operation = next(operation for operation in operations if operation.type == "sync_base")
    assert sync_operation.status == OperationStatus.failed.value
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GIT_PUSH_FAILED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "sync_base_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["operation_id"] == sync_operation.id
    assert push_events[0].payload["operation_type"] == "sync_base"
    assert push_events[0].payload["evidence"]["operation"] == "git push"
    assert push_events[0].payload["evidence"]["returncode"] == 128
    assert "ghp_should_not_persist" not in repr(push_events[0].payload)


@pytest.mark.unit
async def test_fix_cycle_addresses_new_review_burst_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="FALSE POSITIVE: no code change needed")
    adapter.queue(stdout="FALSE POSITIVE: second review is also stale")
    workspace_id = "ws_review_burst"
    first_review = ReviewComment(comment_id="1", body_excerpt="first", author="reviewer")
    second_review = review_node(cid=2, author="reviewer", body="second")
    cmd.queue_result(returncode=0, stdout=pr_payload(reviews=[second_review]))
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        initial_threads=(),
        initial_reviews=(first_review,),
        state=state,
        remote_branch="awf/ws_review_burst",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert state.threads_addressed_ids == {"1": "false_positive", "2": "false_positive"}
    assert len(adapter.calls) == 2
    assert runner._deps.sleep.calls == [30, 30]  # type: ignore[attr-defined]
    assert cmd.calls[-1].args[-2:] == ["origin", "HEAD:refs/heads/awf/ws_review_burst"]


@pytest.mark.unit
async def test_invoke_cli_for_verdict_reports_agent_failed_when_no_changes_committed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(returncode=1, stdout="tool crashed")
    workspace_id = "ws_agent_failed"
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    verdict = await runner._invoke_cli_for_verdict(
        workspace_id=workspace_id,
        prompt="fix it",
        commit_message="fix: review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert verdict == "agent_failed"
    assert cmd.calls[-1].args[-2:] == ["status", "--porcelain"]


@pytest.mark.unit
async def test_sync_base_conflict_invokes_agent_and_pushes_salvaged_resolution(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(returncode=1, stdout="partial conflict resolution")
    workspace_id = "ws_sync_conflict"
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    for result in [
        (0, "", ""),
        (0, "", ""),
        (1, "", "merge conflict"),
        (0, "UU src/conflict.py\n", ""),
        (0, " M src/conflict.py\n", ""),
        (0, "", ""),
        (1, "", ""),
        (0, "", ""),
        (0, "", ""),
    ]:
        cmd.queue_result(returncode=result[0], stdout=result[1], stderr=result[2])
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._run_sync_base(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch="awf/ws_sync_conflict",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert len(adapter.calls) == 1
    assert "src/conflict.py" in adapter.calls[0]
    assert [call.args[-2:] for call in cmd.calls[:2]] == [
        ["merge", "--abort"],
        ["origin", "+refs/heads/development:refs/remotes/origin/development"],
    ]
    assert cmd.calls[-1].args[-2:] == ["origin", "HEAD:refs/heads/awf/ws_sync_conflict"]


@pytest.mark.unit
async def test_ci_fix_records_agent_failure_but_commits_and_pushes_changes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(returncode=1, stdout="format failed")
    workspace_id = "ws_ci_fix"
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    for result in [
        (0, " M tests/test_app.py\n", ""),
        (0, "", ""),
        (1, "", ""),
        (0, "", ""),
        (0, "", ""),
    ]:
        cmd.queue_result(returncode=result[0], stdout=result[1], stderr=result[2])
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="assert 1 == 2"),),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch="awf/ws_ci_fix",
    )

    assert len(adapter.calls) == 1
    assert "assert 1 == 2" in adapter.calls[0]
    assert cmd.calls[-1].args[-2:] == ["origin", "HEAD:refs/heads/awf/ws_ci_fix"]


@pytest.mark.unit
async def test_git_helpers_handle_bad_base_count_and_push_rejection_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="rev-list failed")
    cmd.queue_result(returncode=0, stdout="not an int\n")
    cmd.queue_result(returncode=1, stderr="[rejected] non-fast-forward")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / "ws_git"

    with pytest.raises(BaseBehindCountError):
        await runner._count_base_behind(worktree_path=worktree, base_branch="main")
    with pytest.raises(BaseBehindCountError):
        await runner._count_base_behind(worktree_path=worktree, base_branch="main")
    assert await runner._git_push(worktree_path=worktree, remote_branch="awf/ws_git") is False

    assert cmd.calls[-2].args[-2:] == ["origin", "awf/ws_git"]
    assert cmd.calls[-1].args[-2:] == ["--hard", "origin/awf/ws_git"]


@pytest.mark.unit
async def test_fetch_base_repairs_multiple_broken_awf_refs_before_failing_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fetch_base_once = mocker.patch.object(
        runner,
        "_fetch_base_once",
        mocker.AsyncMock(
            side_effect=[
                CommandResult(returncode=1, stdout="", stderr="bad ref ws_old_1"),
                CommandResult(returncode=1, stdout="", stderr="bad ref ws_old_2"),
                CommandResult(returncode=0, stdout="", stderr=""),
            ]
        ),
    )
    repair = mocker.patch.object(
        runner,
        "_repair_orphaned_broken_awf_ref",
        mocker.AsyncMock(side_effect=[True, True]),
    )

    await runner._fetch_base(
        workspace_id="ws_current",
        worktree_path=tmp_path / "worktrees" / "ws_current",
        base_branch="development",
    )

    assert fetch_base_once.await_count == 3
    assert repair.await_count == 2
    assert [call.kwargs["stderr"] for call in repair.await_args_list] == [
        "bad ref ws_old_1",
        "bad ref ws_old_2",
    ]


@pytest.mark.unit
async def test_fetch_base_wraps_broken_ref_repair_exceptions_as_base_fetch_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fetch_base_once = mocker.patch.object(
        runner,
        "_fetch_base_once",
        mocker.AsyncMock(
            return_value=CommandResult(
                returncode=1,
                stdout="",
                stderr="fatal: bad object refs/heads/awf/ws_old",
            )
        ),
    )
    repair = mocker.patch.object(
        runner,
        "_repair_orphaned_broken_awf_ref",
        mocker.AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(BaseFetchError, match="broken AWF ref repair failed") as exc:
        await runner._fetch_base(
            workspace_id="ws_current",
            worktree_path=tmp_path / "worktrees" / "ws_current",
            base_branch="development",
        )

    assert "database unavailable" in str(exc.value)
    assert fetch_base_once.await_count == 1
    repair.assert_awaited_once()


@pytest.mark.unit
async def test_missing_workspace_terminal_helpers_return_without_side_effects(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(RuntimeError, match="disappeared"):
        await runner._load_workspace("ws_missing")
    await runner._persist_state("ws_missing", MonitorState(last_push_sha="abc"))
    await runner._terminate_failed("ws_missing", message="missing")
    await runner._terminate_completed("ws_missing", pr_merge_sha="abc")


@pytest.mark.unit
@pytest.mark.parametrize(
    "operator_status",
    [
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroying,
        WorkspaceStatus.destroyed,
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
    ],
)
@pytest.mark.parametrize("callback", ["completed", "failed"])
async def test_stale_monitor_terminal_callbacks_do_not_override_operator_states(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    operator_status: WorkspaceStatus,
    callback: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        if operator_status == WorkspaceStatus.cancelled:
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code="OPERATOR_CANCEL",
            )
        elif operator_status in {WorkspaceStatus.destroying, WorkspaceStatus.destroyed}:
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code="OPERATOR_CANCEL",
            )
            await repo.transition(
                workspace,
                to=WorkspaceStatus.destroying,
                reason_code="OPERATOR_DESTROY",
            )
            if operator_status == WorkspaceStatus.destroyed:
                await repo.transition(
                    workspace,
                    to=WorkspaceStatus.destroyed,
                    reason_code="OPERATOR_DESTROY",
                )
        else:
            await repo.transition(
                workspace,
                to=operator_status,
                reason_code="MONITOR_TERMINAL",
            )
        await s.commit()
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    reconcile_calls: list[tuple[str, str, str]] = []
    gc_calls: list[str] = []

    async def _record_reconcile_call(
        *,
        workspace_id: str,
        repo_url: str,
        base_branch: str,
    ) -> None:
        reconcile_calls.append((workspace_id, repo_url, base_branch))

    async def _record_gc_call(workspace_id: str) -> None:
        gc_calls.append(workspace_id)

    runner._reconcile_target_branch_after_merge = _record_reconcile_call  # type: ignore[method-assign]
    runner._gc_completed_workspace_filesystem = _record_gc_call  # type: ignore[method-assign]

    if callback == "completed":
        await runner._terminate_completed(
            workspace_id,
            pr_merge_sha="stale-merge-sha",
            repo_url="git@github.com:example/repo.git",
            base_branch="development",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
    else:
        await runner._terminate_failed(
            workspace_id,
            message="stale monitor failure",
            reason_code="STALE_MONITOR",
        )

    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        ignored_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.stale_callback_ignored"
        ]
        monitor_terminal_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.monitor_terminal_ignored"
        ]

    assert workspace.status == operator_status.value
    assert workspace.pr_merge_sha is None
    assert workspace.failure_reason is None
    assert workspace.failure_message is None
    assert cmd.calls == []
    assert reconcile_calls == []
    assert gc_calls == []
    assert monitor_terminal_events == []
    assert ignored_events[-1].payload == {
        "callback_source": "pr_monitor",
        "callback_action": "terminal_completed" if callback == "completed" else "terminal_failed",
        "expected_status": WorkspaceStatus.monitoring_pr.value,
        "actual_status": operator_status.value,
        "requested_status": "completed" if callback == "completed" else "failed",
        "reason_code": "MONITOR_DONE" if callback == "completed" else "STALE_MONITOR",
    }


@pytest.mark.unit
async def test_load_and_persist_state_convert_monitor_timestamps(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    started_key = _initial_review_grace_started_key(42)
    settle_key = _non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha="head-a",
    )
    wall_started = datetime(2026, 4, 27, 12, 0, tzinfo=UTC).timestamp()
    await _update_workspace(
        factory,
        workspace_id,
        monitor_started_at=datetime(2026, 4, 27, 12, 0),
        monitor_threads_addressed={
            started_key: f"{wall_started:.6f}",
            settle_key: f"{wall_started:.6f}",
        },
        monitor_last_commit_sha="oldsha",
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    ws = await runner._load_workspace(workspace_id)
    state = runner._load_state(ws)
    state.last_push_sha = "newsha"
    state.mark_addressed("review-1", "false_positive")
    await runner._persist_state(workspace_id, state)

    assert float(state.threads_addressed_ids[started_key]) < 1_000_000_000
    assert float(state.threads_addressed_ids[settle_key]) < 1_000_000_000
    async with factory() as s:
        persisted = await WorkspaceRepository(s).get(workspace_id)
        assert persisted is not None
        assert persisted.monitor_last_commit_sha == "newsha"
        assert persisted.monitor_threads_addressed["review-1"] == "false_positive"
        assert float(persisted.monitor_threads_addressed[started_key]) >= 1_000_000_000
        assert float(persisted.monitor_threads_addressed[settle_key]) >= 1_000_000_000


@pytest.mark.unit
async def test_load_and_persist_state_handles_workspace_without_pr_number(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _update_workspace(
        factory,
        workspace_id,
        pr_number=None,
        monitor_started_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        monitor_threads_addressed={"review-1": "false_positive"},
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    ws = await runner._load_workspace(workspace_id)
    state = runner._load_state(ws)
    state.iter_count = 3
    await runner._persist_state(workspace_id, state)

    async with factory() as s:
        persisted = await WorkspaceRepository(s).get(workspace_id)
        assert persisted is not None
        assert persisted.monitor_iter_count == 3
        assert persisted.monitor_threads_addressed == {"review-1": "false_positive"}


@pytest.mark.unit
def test_load_state_normalizes_naive_started_at_without_database(
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=None,  # type: ignore[arg-type]
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    workspace = Workspace(
        id="ws_naive_started",
        status=WorkspaceStatus.monitoring_pr.value,
        repo_url="git@github.com:dimileeh/aira-web.git",
        branch_base="development",
        task_title="naive",
        task_prompt="x",
        agent="claude_code",
        test_commands=[],
        monitor_started_at=datetime(2026, 4, 27, 12, 0),
        pr_number=None,
    )

    state = runner._load_state(workspace)
    aware_workspace = Workspace(
        id="ws_aware_started",
        status=WorkspaceStatus.monitoring_pr.value,
        repo_url="git@github.com:dimileeh/aira-web.git",
        branch_base="development",
        task_title="aware",
        task_prompt="x",
        agent="claude_code",
        test_commands=[],
        monitor_started_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        pr_number=None,
    )
    aware_state = runner._load_state(aware_workspace)

    assert state.started_at > 0
    assert aware_state.started_at > 0


@pytest.mark.unit
async def test_terminate_completed_without_optional_merge_cleanup_inputs(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._terminate_completed(workspace_id, pr_merge_sha=None)

    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.completed.value


@pytest.mark.unit
async def test_compose_teardown_runner_exception_is_swallowed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.runner = _ExplodingRunner()  # type: ignore[assignment]

    assert (
        await runner._teardown_compose_stack(
            workspace_id="ws_teardown",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        is False
    )


@pytest.mark.unit
def test_stale_pending_check_warning_helpers_cover_disabled_and_terminal_cases() -> None:
    started = datetime.now(UTC) - timedelta(seconds=120)
    pending = CheckTiming(name="slow", status="IN_PROGRESS", started_at=started)
    terminal_status = CheckTiming(name="done", status="COMPLETED", started_at=started)
    terminal_conclusion = CheckTiming(
        name="done-conclusion",
        status="MYSTERY",
        conclusion="SUCCESS",
        started_at=started,
    )
    missing_started = CheckTiming(name="missing-started", status="IN_PROGRESS")

    assert (
        _stale_pending_check_warnings(
            _status_for_helpers(checks=(pending,)),
            now=datetime.now(UTC),
            threshold_seconds=0,
        )
        == ()
    )
    assert (
        _stale_pending_check_warnings(
            _status_for_helpers(checks=(terminal_status, terminal_conclusion, missing_started)),
            now=datetime.now(UTC),
            threshold_seconds=60,
        )
        == ()
    )
    warnings = _stale_pending_check_warnings(
        _status_for_helpers(checks=(pending,)),
        now=datetime.now(UTC),
        threshold_seconds=60,
    )
    assert len(warnings) == 1
    assert warnings[0].check_name == "slow"
    assert warnings[0].threshold_window >= 1


@pytest.mark.unit
def test_notify_human_reason_and_merge_rejection_detail() -> None:
    blocking_review = ReviewComment(
        comment_id="C1",
        body_excerpt="review skipped",
        author="coderabbitai",
        blocks_merge=True,
    )
    status = _status_for_helpers(reviews=(blocking_review,))

    assert "review bot reported" in (_notify_human_reason(status, MonitorState()) or "")
    blocked = _status_for_helpers()
    blocked = PRStatus(
        number=blocked.number,
        head_sha=blocked.head_sha,
        mergeable=blocked.mergeable,
        check_state=blocked.check_state,
        unresolved_inline_threads=blocked.unresolved_inline_threads,
        unresolved_review_comments=blocked.unresolved_review_comments,
        base_behind_count=blocked.base_behind_count,
        merge_state_status=MergeStateStatus.BLOCKED,
    )
    assert "GitHub reports merge state BLOCKED" in (
        _notify_human_reason(blocked, MonitorState()) or ""
    )
    assert _merge_rejection_reason("") == "GitHub rejected the merge attempt"
    assert _merge_rejection_reason("  protected\n branch  ") == (
        "GitHub rejected the merge attempt: protected branch"
    )


@pytest.mark.unit
def test_initial_review_grace_state_converts_wall_and_legacy_values() -> None:
    started_key = _initial_review_grace_started_key(42)
    done_key = _initial_review_grace_done_key(42)
    wall_started = datetime(2026, 4, 27, 12, 0, tzinfo=UTC).timestamp()

    runtime_state = _initial_review_grace_state_for_runtime(
        {started_key: f"{wall_started:.6f}", done_key: "elapsed"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 60,
    )
    assert runtime_state[started_key] == "1040.000000"
    assert runtime_state[done_key] == "elapsed"

    legacy_state = _initial_review_grace_state_for_runtime(
        {started_key: "500.0"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 60,
        legacy_monotonic_fallback=900,
    )
    assert legacy_state[started_key] == "900.000000"

    persisted = _initial_review_grace_state_for_persistence(
        {started_key: "1040.000000"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 60,
    )
    assert persisted[started_key] == f"{wall_started:.6f}"

    already_wall = _initial_review_grace_state_for_persistence(
        {started_key: f"{wall_started:.6f}"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 60,
    )
    assert already_wall[started_key] == f"{wall_started:.6f}"

    invalid = _initial_review_grace_state_for_persistence(
        {started_key: "not-a-number"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started,
    )
    assert invalid[started_key] == "not-a-number"

    wait_state = MonitorState(
        started_at=123.0,
        threads_addressed_ids={started_key: object()},  # type: ignore[dict-item]
    )
    assert (
        _initial_review_grace_wait_seconds(
            wait_state,
            pr_number=42,
            now=124.0,
            grace_seconds=10,
            poll_interval_seconds=2,
        )
        == 2
    )
    assert wait_state.threads_addressed_ids[started_key] == "123.000000"
    assert (
        _initial_review_grace_wall_started_value_from_datetime(datetime(2026, 4, 27, 12, 0))
        == f"{wall_started:.6f}"
    )


@pytest.mark.unit
def test_non_check_reviewer_settle_state_converts_wall_legacy_and_invalid_values() -> None:
    started_key = _non_check_reviewer_settle_started_key(pr_number=42, head_sha="head-1")
    legacy_key = f"{started_key}:legacy"
    invalid_key = f"{started_key}:invalid"
    other_key = _non_check_reviewer_settle_started_key(pr_number=99, head_sha="head-2")
    wall_started = datetime(2026, 4, 27, 12, 0, tzinfo=UTC).timestamp()

    runtime_state = _non_check_reviewer_settle_state_for_runtime(
        {
            started_key: f"{wall_started:.6f}",
            legacy_key: "500.0",
            invalid_key: "not-a-number",
            other_key: "untouched",
            "review:1": "addressed",
        },
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 75,
    )
    assert runtime_state[started_key] == "1025.000000"
    assert runtime_state[legacy_key] == "1100.000000"
    assert runtime_state[invalid_key] == "not-a-number"
    assert runtime_state[other_key] == "untouched"
    assert runtime_state["review:1"] == "addressed"

    persisted_state = _non_check_reviewer_settle_state_for_persistence(
        runtime_state,
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 75,
    )
    assert persisted_state[started_key] == f"{wall_started:.6f}"
    assert persisted_state[legacy_key] == f"{wall_started + 75:.6f}"
    assert persisted_state[invalid_key] == "not-a-number"
    assert persisted_state[other_key] == "untouched"
    assert persisted_state["review:1"] == "addressed"

    invalid_marker = object()
    runtime_invalid_object = _non_check_reviewer_settle_state_for_runtime(
        {started_key: invalid_marker},  # type: ignore[dict-item]
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started,
    )
    assert runtime_invalid_object[started_key] is invalid_marker

    persisted_wall = _non_check_reviewer_settle_state_for_persistence(
        {started_key: f"{wall_started:.6f}"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 75,
    )
    assert persisted_wall[started_key] == f"{wall_started:.6f}"

    persisted_legacy = _non_check_reviewer_settle_state_for_persistence(
        {started_key: "1040.000000"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started + 60,
    )
    assert persisted_legacy[started_key] == f"{wall_started:.6f}"

    persisted_invalid = _non_check_reviewer_settle_state_for_persistence(
        {started_key: "not-a-number"},
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started,
    )
    assert persisted_invalid[started_key] == "not-a-number"

    persisted_invalid_marker = object()
    persisted_invalid_object = _non_check_reviewer_settle_state_for_persistence(
        {started_key: persisted_invalid_marker},  # type: ignore[dict-item]
        pr_number=42,
        now_monotonic=1_100,
        now_wall_seconds=wall_started,
    )
    assert persisted_invalid_object[started_key] is persisted_invalid_marker


@pytest.mark.unit
def test_pending_check_and_defer_helpers_cover_unknown_and_review_paths() -> None:
    assert _is_pending_check(CheckTiming(name="custom", status="waiting-on-provider"))
    assert not _is_pending_check(CheckTiming(name="terminal", conclusion="cancelled"))
    assert _as_utc(datetime(2026, 4, 27, 12, 0)).tzinfo is UTC
    bot_thread = ReviewThread(
        thread_id="T1",
        path="src/a.py",
        line=10,
        body_excerpt="nit",
        author="coderabbitai",
    )
    human_review = ReviewComment(
        comment_id="R1",
        body_excerpt="needs a maintainer",
        author="octocat",
    )
    ignored_review = ReviewComment(
        comment_id="R2",
        body_excerpt="not deferred",
        author="octocat",
    )
    state = MonitorState(
        threads_addressed_ids={
            "T1": "defer",
            "R1": "defer",
        }
    )

    bot_items, human_items = _collect_defer_items(
        _status_for_helpers(threads=(bot_thread,), reviews=(human_review, ignored_review)),
        state,
    )

    assert [item["id"] for item in bot_items] == ["T1"]
    assert [item["kind"] for item in human_items] == ["review"]
    assert (
        _notify_human_reason(
            _status_for_helpers(reviews=(human_review,)),
            state,
        )
        == "human review feedback was deferred by the agent and remains unresolved"
    )
    with_failures = _with_ci_failures(
        _status_for_helpers(),
        (CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="failed"),),
    )
    assert with_failures.ci_failures[0].name == "pytest"


@pytest.mark.unit
def test_target_reconcile_payload_supports_dict_to_dict_and_fallback() -> None:
    class _ToDict:
        def to_dict(self) -> dict[str, object]:
            return {"status": "clean"}

    class _BadToDict:
        def to_dict(self) -> list[str]:
            return ["not", "a", "dict"]

    class _NonCallableToDict:
        to_dict = {"status": "not callable"}

    bad = _BadToDict()
    assert _target_reconcile_payload({"status": "ok"}) == {"status": "ok"}
    assert _target_reconcile_payload(_ToDict()) == {"status": "clean"}
    assert _target_reconcile_payload(bad) == {"result": str(bad)}
    non_callable = _NonCallableToDict()
    assert _target_reconcile_payload(non_callable) == {"result": str(non_callable)}


@pytest.mark.unit
def test_candidate_stale_required_action_maps_validation_reason() -> None:
    assert _candidate_stale_required_action(None) is None
    assert _candidate_stale_required_action("validation_insufficient_tier") == "validate"
    assert _candidate_stale_required_action("STALE_TARGET_ADVANCED") == "rebase"


@pytest.mark.unit
def test_git_push_and_porcelain_helpers_cover_clean_rename_and_invalid_lines() -> None:
    clean_push = _GitPushResult(pushed=False, failed=False, returncode=0)
    assert clean_push.error_message is None

    assert _changed_paths_from_porcelain(
        "\n"
        "not porcelain\n"
        " M src/changed.py\n"
        "?? docs/new.md\n"
        "R  old/name.py -> src/name.py\n"
        " M src/changed.py\n"
    ) == [
        "src/changed.py",
        "docs/new.md",
        "old/name.py",
        "src/name.py",
    ]


@pytest.mark.unit
def test_validation_head_helper_requires_current_successful_matching_attempt() -> None:
    workspace = Workspace(
        id="ws_validation_helper",
        status=WorkspaceStatus.monitoring_pr.value,
        repo_url="git@github.com:dimileeh/aira-web.git",
        branch_base="development",
        task_title="validation helper",
        task_prompt="x",
        agent="claude_code",
        test_commands=[],
    )
    workspace.validation_runs = [
        ValidationRun(
            id="vr_wrong_attempt",
            workspace_id=workspace.id,
            attempt_id="attempt-other",
            tier=1,
            command_set_hash="hash",
            commands=[],
            status="succeeded",
            workspace_head_sha="workspace-head",
            target_head_sha="target-head",
        ),
        ValidationRun(
            id="vr_failed",
            workspace_id=workspace.id,
            attempt_id="attempt-1",
            tier=1,
            command_set_hash="hash",
            commands=[],
            status="failed",
            workspace_head_sha="workspace-head",
            target_head_sha="target-head",
        ),
        ValidationRun(
            id="vr_success",
            workspace_id=workspace.id,
            attempt_id="attempt-1",
            tier=1,
            command_set_hash="hash",
            commands=[],
            status="succeeded",
            workspace_head_sha="workspace-head",
            target_head_sha="target-head",
        ),
    ]

    assert not _has_successful_validation_for_pr_head(
        workspace,
        attempt_id="attempt-1",
        current_head_sha=None,
    )
    assert not _has_successful_validation_for_pr_head(
        workspace,
        attempt_id="attempt-1",
        current_head_sha="unvalidated-head",
    )
    assert _has_successful_validation_for_pr_head(
        workspace,
        attempt_id="attempt-1",
        current_head_sha="workspace-head",
    )
    assert _has_successful_validation_for_pr_head(
        workspace,
        attempt_id="attempt-1",
        current_head_sha="target-head",
    )


@pytest.mark.unit
async def test_merge_gate_legacy_head_support_modern_fallback_and_typeerror(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    modern_calls: list[tuple[str, bool, str | None]] = []
    sentinel = object()

    async def modern_gate(
        workspace_id: str,
        *,
        check_policy: bool = False,
        current_head_sha: str | None = None,
    ) -> object:
        modern_calls.append((workspace_id, check_policy, current_head_sha))
        return sentinel

    monkeypatch.setattr(runner, "_merge_gate_for_workspace", modern_gate)
    assert (
        await runner._merge_gate_with_legacy_head_support(
            "ws_modern",
            check_policy=True,
            current_head_sha="head1",
        )
        is sentinel
    )
    assert modern_calls == [("ws_modern", True, "head1")]

    plain_calls: list[tuple[str, bool]] = []

    async def plain_gate(
        workspace_id: str,
        *,
        check_policy: bool = False,
    ) -> object:
        plain_calls.append((workspace_id, check_policy))
        return sentinel

    monkeypatch.setattr(runner, "_merge_gate_for_workspace", plain_gate)
    assert (
        await runner._merge_gate_with_legacy_head_support(
            "ws_plain",
            check_policy=True,
            current_head_sha=None,
        )
        is sentinel
    )
    assert plain_calls == [("ws_plain", True)]

    legacy_calls: list[tuple[str, bool]] = []

    async def legacy_gate(
        workspace_id: str,
        *,
        check_policy: bool = False,
    ) -> object:
        legacy_calls.append((workspace_id, check_policy))
        return sentinel

    monkeypatch.setattr(runner, "_merge_gate_for_workspace", legacy_gate)
    assert (
        await runner._merge_gate_with_legacy_head_support(
            "ws_legacy",
            check_policy=True,
            current_head_sha="head2",
        )
        is sentinel
    )
    assert legacy_calls == [("ws_legacy", True)]

    async def unrelated_type_error(
        workspace_id: str,
        *,
        check_policy: bool = False,
        current_head_sha: str | None = None,
    ) -> object:
        del workspace_id, check_policy, current_head_sha
        raise TypeError("candidate relation is unavailable")

    monkeypatch.setattr(runner, "_merge_gate_for_workspace", unrelated_type_error)
    with pytest.raises(TypeError, match="candidate relation"):
        await runner._merge_gate_with_legacy_head_support(
            "ws_error",
            current_head_sha="head3",
        )


@pytest.mark.unit
async def test_non_check_reviewer_settle_ignores_unknown_waiting_and_unchanged_decisions(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        non_check_reviewer_settle_seconds=120,
    )
    status = _status_for_helpers()

    await runner._record_non_check_reviewer_settle_decision(
        decision=_NonCheckReviewerSettleDecision(action="unrecognized", state_changed=True),
        workspace_id=workspace_id,
        pr_number=42,
        status=status,
        monitor_log=None,
    )
    await runner._record_non_check_reviewer_settle_decision(
        decision=_NonCheckReviewerSettleDecision(action="waiting", state_changed=True),
        workspace_id=workspace_id,
        pr_number=42,
        status=status,
        monitor_log=None,
    )
    await runner._record_non_check_reviewer_settle_decision(
        decision=_NonCheckReviewerSettleDecision(action="started", state_changed=False),
        workspace_id=workspace_id,
        pr_number=42,
        status=status,
        monitor_log=None,
    )

    async with factory() as s:
        events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.non_check_reviewer_settle",
            limit=10,
        )
    assert events == []


@pytest.mark.unit
async def test_provider_recovery_suppression_blocks_all_monitor_agent_invocations(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def suppressed(_workspace_id: str) -> bool:
        return True

    monkeypatch.setattr(runner, "_provider_recovery_suppresses_cli", suppressed)
    with pytest.raises(ProviderRecoveryRetryError):
        await runner._invoke_cli_for_verdict(
            workspace_id="ws_suppressed",
            prompt="fix review",
            commit_message="fix: review",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
    with pytest.raises(ProviderRecoveryRetryError):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="boom"),),
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            workspace_id="ws_suppressed",
            remote_branch="awf/ws_suppressed",
        )

    cmd.queue_result(returncode=0)  # git merge --abort
    cmd.queue_result(returncode=0)  # git fetch origin development
    cmd.queue_result(returncode=1, stderr="conflict")  # git merge
    cmd.queue_result(returncode=0, stdout="UU src/conflict.py\n")  # git status
    with pytest.raises(ProviderRecoveryRetryError):
        await runner._run_sync_base(
            workspace_id="ws_suppressed",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            base_branch="development",
            remote_branch="awf/ws_suppressed",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
    assert adapter.calls == []


@pytest.mark.unit
async def test_protected_scope_repair_returns_none_when_recheck_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await session.commit()

    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="removed workflow edit")
    cmd.queue_result(returncode=128, stderr="fatal: not a git repository")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert (
        await runner._repair_protected_scope_changes_before_commit(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        is None
    )
    assert len(adapter.calls) == 1


@pytest.mark.unit
async def test_commit_dirty_worktree_stops_when_protected_scope_repair_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_fails(**_kwargs: object) -> object | None:
        return None

    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_fails,
    )

    assert not await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: repair protected scope",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )
    assert len(cmd.calls) == 1


@pytest.mark.unit
async def test_protected_scope_repair_raises_provider_retry_before_cli(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await session.commit()

    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch.object(
        runner,
        "_provider_recovery_suppresses_cli",
        mocker.AsyncMock(return_value=True),
    )

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._repair_protected_scope_changes_before_commit(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
    assert adapter.calls == []


@pytest.mark.unit
async def test_protected_scope_violations_skip_empty_status(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert (
        await runner._protected_scope_violations_for_status(
            workspace_id="ws_without_changes",
            status_stdout="",
        )
        == []
    )


@pytest.mark.unit
async def test_protected_scope_repair_records_remaining_violations_after_agent_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await session.commit()

    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(returncode=1, stdout="tool crashed before cleanup")
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert (
        await runner._repair_protected_scope_changes_before_commit(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        is None
    )

    async with factory() as s:
        events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_repair_failed",
            limit=10,
        )
    assert len(events) == 1
    assert events[0].reason_code == "PROTECTED_SCOPE_REPAIR_FAILED"
    assert events[0].payload is not None
    assert events[0].payload["paths"] == [".github/workflows/ci.yml"]
