"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_mock
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import ValidationRun, Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner import constants as pr_monitor_runner_constants
from awf.runtime.pr_monitor_runner import lifecycle as pr_lifecycle
from awf.runtime.pr_monitor_runner import recovery_payloads as pr_monitor_runner_recovery_payloads
from awf.runtime.pr_monitor_runner.gates import (
    _has_successful_validation_for_pr_head,
    _NonCheckReviewerSettleDecision,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _as_utc,
    _candidate_stale_required_action,
    _changed_paths_from_porcelain,
    _changed_paths_from_porcelain_z,
    _collect_defer_items,
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _initial_review_grace_state_for_persistence,
    _initial_review_grace_state_for_runtime,
    _initial_review_grace_wait_seconds,
    _initial_review_grace_wall_started_value_from_datetime,
    _is_pending_check,
    _is_protected_manual_ready_handoff,
    _merge_rejection_reason,
    _non_check_reviewer_settle_started_key,
    _non_check_reviewer_settle_state_for_persistence,
    _non_check_reviewer_settle_state_for_runtime,
    _notify_human_reason,
    _stale_pending_check_warnings,
    _target_reconcile_payload,
    _untracked_paths_from_porcelain,
    _untracked_paths_from_porcelain_z,
    _with_ci_failures,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryRetryError,
)
from awf.runtime.pr_push_remote import (
    remote_push_url_for_workspace as _remote_push_url_for_workspace,
)
from awf.service.merge_queue import MergeQueueBlocker
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
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


_PROTECTED_WORKFLOW_OLD = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
  lint:
    runs-on: ubuntu-latest
    steps:
      - name: Run ruff
        run: uv run ruff check
""".strip()
_PROTECTED_WORKFLOW_BLOCKED = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: true
""".strip()


def _queue_protected_workflow_diff(
    cmd: FakeCommandRunner,
    *,
    old_text: str = _PROTECTED_WORKFLOW_OLD,
    new_text: str = _PROTECTED_WORKFLOW_BLOCKED,
) -> None:
    cmd.queue_result(returncode=0)  # cat-file base:path
    cmd.queue_result(returncode=0, stdout=old_text)
    cmd.queue_result(returncode=0)  # cat-file HEAD:path
    cmd.queue_result(returncode=0, stdout=new_text)


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


def _assert_committed_diff_phase_ran(
    cmd: FakeCommandRunner,
    *,
    worktree_path: Path,
    remote_branch: str,
    remote: str = "origin",
) -> None:
    call_args = [call.args for call in cmd.calls]
    assert (
        _git_worktree_command(
            worktree_path,
            "fetch",
            remote,
            f"refs/heads/{remote_branch}",
        )
        in call_args
    )
    assert (
        _git_worktree_command(
            worktree_path,
            "merge-base",
            "FETCH_HEAD",
            "HEAD",
        )
        in call_args
    )


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


def _name_status_z(*paths: str) -> str:
    return "".join(f"M\0{path}\0" for path in paths)


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
    if operator_status == WorkspaceStatus.completed and callback == "completed":
        assert workspace.pr_merge_sha == "stale-merge-sha"
    else:
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
    mocker: pytest_mock.MockerFixture,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> datetime:
            assert tz is UTC
            return datetime(2026, 4, 27, 12, 1, tzinfo=UTC)

    mocker.patch.object(pr_lifecycle, "datetime", FrozenDateTime)
    mocker.patch("time.monotonic", return_value=30.0)

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

    assert state.started_at == pytest.approx(-30.0)
    assert aware_state.started_at == pytest.approx(-30.0)


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

    assert (
        _notify_human_reason(status, MonitorState())
        == "a merge-blocking changes-requested review remains unresolved"
    )
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
def test_protected_manual_ready_handoff_rejects_blocking_review_comments() -> None:
    blocking_review = ReviewComment(
        comment_id="C1",
        body_excerpt="still blocking",
        author="reviewer",
        blocks_merge=True,
    )
    base = _status_for_helpers(reviews=(blocking_review,))
    status = PRStatus(
        number=base.number,
        head_sha=base.head_sha,
        mergeable=base.mergeable,
        check_state=base.check_state,
        unresolved_inline_threads=base.unresolved_inline_threads,
        unresolved_review_comments=base.unresolved_review_comments,
        blocking_reviews=base.blocking_reviews,
        base_behind_count=base.base_behind_count,
        merge_state_status=MergeStateStatus.BLOCKED,
    )

    assert _is_protected_manual_ready_handoff(status, MonitorState()) is False


@pytest.mark.unit
def test_monitor_recovery_conformance_payload_normalizes_matching_handoff() -> None:
    assert (
        pr_monitor_runner_recovery_payloads._normalize_conformance_handoff_reason_code(None) is None
    )  # noqa: SLF001
    assert (
        pr_monitor_runner_recovery_payloads._normalize_conformance_handoff_reason_code("  ") is None
    )  # noqa: SLF001
    assert (
        pr_monitor_runner_recovery_payloads._normalize_conformance_handoff_reason_code(  # noqa: SLF001
            "conformance-requires-awf-validation"
        )
        == "CONFORMANCE_REQUIRES_AWF_VALIDATION"
    )

    workspace = SimpleNamespace(
        events=[
            SimpleNamespace(event_type="workspace.unrelated", payload=None, reason_code=None),
            SimpleNamespace(
                event_type=pr_monitor_runner_constants._PLANNING_VALIDATION_HANDOFF_EVENT,  # noqa: SLF001
                payload={
                    "report_reason_code": "conformance-requires-awf-validation",
                    "summary": "validation evidence missing",
                    "gaps": ["rerun AWF validation"],
                    "plan_path": "plans/example.md",
                    "iteration": 2,
                },
                reason_code=None,
            ),
        ]
    )

    assert pr_monitor_runner_recovery_payloads._monitor_recovery_conformance_payload(workspace) == {  # noqa: SLF001
        "conformance": {
            "reason_code": "CONFORMANCE_REQUIRES_AWF_VALIDATION",
            "report_reason_code": "CONFORMANCE_REQUIRES_AWF_VALIDATION",
            "summary": "validation evidence missing",
            "gaps": ["rerun AWF validation"],
            "plan_path": "plans/example.md",
            "iteration": 2,
        }
    }


@pytest.mark.unit
def test_monitor_recovery_conformance_payload_stops_after_satisfied_event() -> None:
    workspace = SimpleNamespace(
        events=[
            SimpleNamespace(
                event_type=pr_monitor_runner_constants._PLANNING_VALIDATION_HANDOFF_EVENT,  # noqa: SLF001
                payload={"reason_code": "CONFORMANCE_REQUIRES_AWF_VALIDATION"},
                reason_code=None,
            ),
            SimpleNamespace(
                event_type=pr_monitor_runner_constants._POST_VALIDATION_CONFORMANCE_SATISFIED_EVENT,  # noqa: SLF001
                payload=None,
                reason_code=None,
            ),
        ]
    )

    assert (
        pr_monitor_runner_recovery_payloads._monitor_recovery_conformance_payload(workspace) is None
    )  # noqa: SLF001


@pytest.mark.unit
def test_monitor_recovery_conformance_payload_ignores_non_matching_reason() -> None:
    workspace = SimpleNamespace(
        events=[
            SimpleNamespace(
                event_type=pr_monitor_runner_constants._PLANNING_VALIDATION_HANDOFF_EVENT,  # noqa: SLF001
                payload={"reason_code": "PLAN_CONFORMANCE_UNSATISFIED"},
                reason_code=None,
            )
        ]
    )

    assert (
        pr_monitor_runner_recovery_payloads._monitor_recovery_conformance_payload(workspace) is None
    )  # noqa: SLF001


@pytest.mark.unit
def test_protected_manual_ready_handoff_requires_protected_merge_state() -> None:
    clean = _status_for_helpers()
    clean_status = PRStatus(
        number=clean.number,
        head_sha=clean.head_sha,
        mergeable=clean.mergeable,
        check_state=clean.check_state,
        unresolved_inline_threads=clean.unresolved_inline_threads,
        unresolved_review_comments=clean.unresolved_review_comments,
        base_behind_count=clean.base_behind_count,
        merge_state_status=MergeStateStatus.CLEAN,
    )
    assert _is_protected_manual_ready_handoff(clean_status, MonitorState()) is False

    blocked = PRStatus(
        number=clean.number,
        head_sha=clean.head_sha,
        mergeable=clean.mergeable,
        check_state=clean.check_state,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=clean.base_behind_count,
        merge_state_status=MergeStateStatus.BLOCKED,
    )
    assert _is_protected_manual_ready_handoff(blocked, MonitorState()) is True


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
        ((CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="failed"),), False),
    )
    assert with_failures.ci_failures[0].name == "pytest"


@pytest.mark.unit
def test_notify_human_reason_and_artifact_surface_bot_needs_human_thread() -> None:
    # #305: a bot-authored ``needs_human`` inline thread blocks the merge in
    # ``decide`` but is not "human deferred". ``_notify_human_reason`` must still
    # return a reason (never None -> a false "ready to merge"), and the terminal
    # artifact must include the item rather than silently dropping it.
    bot_thread = ReviewThread(
        thread_id="T_nh",
        path="src/a.py",
        line=3,
        body_excerpt="the diff may be wrong",
        author="coderabbitai",
    )
    state = MonitorState(threads_addressed_ids={"T_nh": "needs_human"})
    status = _status_for_helpers(threads=(bot_thread,))

    reason = _notify_human_reason(status, state)
    assert reason is not None
    assert "needs human input" in reason

    bot_items, human_items = _collect_defer_items(status, state)
    assert [item["id"] for item in bot_items] == ["T_nh"]
    assert bot_items[0]["verdict"] == "needs_human"
    assert human_items == []


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
def test_remote_push_url_for_adopted_fork_preserves_https_credentials() -> None:
    workspace = Workspace(
        id="ws_https_fork_credentials",
        status=WorkspaceStatus.monitoring_pr.value,
        repo_url=("https://x-access-token:credential-value@github.com/base/aira-web.git"),
        branch_base="development",
        branch_name="feature-sync/ws_https_fork_credentials",
        remote_push_branch="fix/review",
        task_title="fork credentials",
        task_prompt="x",
        task_kind="sync_feature_pr",
        task_policy={
            "pr_adoption": {
                "head_repo_slug": "contributor/aira-web",
            }
        },
        agent="claude_code",
        test_commands=[],
    )

    assert _remote_push_url_for_workspace(
        workspace,
        base_repo=RepoRef(owner="base", name="aira-web"),
    ) == ("https://x-access-token:credential-value@github.com/contributor/aira-web.git")


@pytest.mark.unit
def test_git_push_and_porcelain_helpers_cover_clean_rename_and_invalid_lines() -> None:
    clean_push = _GitPushResult(pushed=False, failed=False, returncode=0)
    assert clean_push.error_message is None

    assert _changed_paths_from_porcelain(
        "\n"
        "not porcelain\n"
        " M src/changed.py\n"
        "?? docs/new.md\n"
        ' M "dir a/file b.txt"\n'
        "R  old/name.py -> src/name.py\n"
        'R  "src/old -> backup.py" -> src/new.py\n'
        " M src/changed.py\n"
    ) == [
        "src/changed.py",
        "docs/new.md",
        "dir a/file b.txt",
        "old/name.py",
        "src/name.py",
        "src/old -> backup.py",
        "src/new.py",
    ]
    assert _untracked_paths_from_porcelain(
        '?? old/name.py -> src/name.py\n?? "docs/new note.md"\n?? docs/new.md\n'
        "!! ignored-output.json\n"
        "?? docs/new.md\n M tracked.py\n"
    ) == [
        "old/name.py -> src/name.py",
        "docs/new note.md",
        "docs/new.md",
    ]
    assert _changed_paths_from_porcelain_z(
        "\0".join(
            [
                " M src/changed file.py",
                "?? docs/new note.md",
                "R  src/new name.py",
                "old/name.py",
                "!! ignored.txt",
                "",
            ]
        )
    ) == [
        "src/changed file.py",
        "docs/new note.md",
        "old/name.py",
        "src/new name.py",
    ]
    assert _untracked_paths_from_porcelain_z(
        "?? docs/new note.md\0"
        "!! ignored-output.json\0"
        "?? plans/orphan dir/file one.md\0"
        " M tracked.py\0"
    ) == ["docs/new note.md", "plans/orphan dir/file one.md"]


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

    cmd.queue_result(returncode=0, stdout="abc123\n")
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
    cmd.queue_result(returncode=0)  # cat-file HEAD:.github/workflows/ci.yml
    cmd.queue_result(returncode=0, stdout=_PROTECTED_WORKFLOW_BLOCKED)
    cmd.queue_result(returncode=0, stdout="a" * 40)  # rev-parse HEAD before repair
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
async def test_workspace_test_commands_returns_empty_for_missing_workspace(
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

    assert await runner._workspace_test_commands("ws_missing") == ()
