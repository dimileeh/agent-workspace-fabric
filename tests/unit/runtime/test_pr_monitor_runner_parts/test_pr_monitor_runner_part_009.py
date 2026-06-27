"""Unit tests for focused ``pr_monitor_runner`` behavior.

Most cases cover the pure, side-effect-free helpers: ``_parse_verdict`` (CLI
reply → structured verdict) and ``_collect_defer_items`` (PRStatus +
MonitorState → bot/human defer buckets for the terminal artifact). Focused
runtime-path regressions live here when the unit suite needs to cover a
specific merge-gate branch without running the full monitor integration loop.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_SERVICE_UNHEALTHY
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import (
    AgentRuntime,
    OperationStatus,
    OperationType,
    TaskClass,
    WorkspaceStatus,
)
from awf.db.models import Operation, Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.profiles.models import ProfileDocker, WorkspaceProfile
from awf.runtime.planning import CONFORMANCE_REQUIRES_AWF_VALIDATION
from awf.runtime.pr_monitor import (
    AddressComments,
    CheckFailure,
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
)
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.gates import _MergeGateResult
from awf.runtime.pr_monitor_runner.helpers import (
    _initial_review_grace_started_key,
    _initial_review_grace_wall_started_value_from_datetime,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryRetryError,
    _MonitorAgentServiceRecoveryFailedError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor import _status


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture(autouse=True)
def _mock_verify_head_object_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )


class PersistCheckingSleep(RecordedSleep):
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
        state_key: str,
        expected_value: str,
    ) -> None:
        super().__init__()
        self._factory = factory
        self._workspace_id = workspace_id
        self._state_key = state_key
        self._expected_value = expected_value

    async def __call__(self, seconds: float) -> None:
        async with self._factory() as session:
            workspace = await WorkspaceRepository(session).get(self._workspace_id)
            assert workspace is not None
            assert workspace.monitor_threads_addressed[self._state_key] == self._expected_value
        await super().__call__(seconds)


class PersistCheckingCommandRunner(FakeCommandRunner):
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
        state_key: str,
        expected_value: str,
    ) -> None:
        super().__init__()
        self._factory = factory
        self._workspace_id = workspace_id
        self._state_key = state_key
        self._expected_value = expected_value

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        if args[:3] == ["gh", "run", "rerun"]:
            async with self._factory() as session:
                workspace = await WorkspaceRepository(session).get(self._workspace_id)
                assert workspace is not None
                assert (
                    workspace.monitor_threads_addressed.get(self._state_key) == self._expected_value
                )
        return await super().run(args, input_bytes=input_bytes, cwd=cwd)


def _monitor_runner(
    tmp_path: Path,
    fake: FakeCommandRunner,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    workspace_runtime_context: str = "",
) -> PullRequestMonitorRunner:
    return PullRequestMonitorRunner(
        session_factory=session_factory or object(),  # type: ignore[arg-type]
        runner=fake,
        adapter=object(),  # type: ignore[arg-type]
        gh=object(),  # type: ignore[arg-type]
        worktrees_root=tmp_path / "work" / "git" / "worktrees",
        workspace_runtime_context=workspace_runtime_context,
    )


def _green_status(*, pr_number: int = 42, head_sha: str = "abc1234567890def") -> PRStatus:
    return PRStatus(
        number=pr_number,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


class _CommandIterable:
    def __iter__(self) -> Iterator[object]:
        return iter(("pytest -q", object(), "ruff check ."))


def _gh_pr_merge_calls(cmd: FakeCommandRunner) -> list[list[str]]:
    return [call.args for call in cmd.calls if call.args[:3] == ["gh", "pr", "merge"]]


class _CapturingGH:
    def __init__(self, status: PRStatus | None = None) -> None:
        self.status = status or _green_status()
        self.base_behind_counts: list[int] = []
        self.failing_log_requests: list[tuple[RepoRef, int, str, tuple[str, ...]]] = []
        self.posted_comments: list[tuple[RepoRef, int, str]] = []
        self.post_errors: list[GitHubClientError] = []

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
    ) -> PRStatus:
        del repo, pr_number
        self.base_behind_counts.append(base_behind_count)
        return replace(self.status, base_behind_count=base_behind_count)

    async def fetch_failing_check_logs(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        head_sha: str,
        pytest_fallback_commands: Sequence[str] = (),
        rollup_checks: object = (),
    ) -> tuple[CheckFailure, ...]:
        del rollup_checks
        self.failing_log_requests.append(
            (repo, pr_number, head_sha, tuple(pytest_fallback_commands))
        )
        return ()

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        if self.post_errors:
            raise self.post_errors.pop(0)
        self.posted_comments.append((repo, pr_number, body))


def _provider_recovery_policy(
    *,
    fallback_agent: str = "codex",
    fallback_provider: str = "openai",
    fallback_model: str = "gpt-5.3-codex",
    max_same_provider_retries: int = 1,
) -> dict[str, object]:
    return {
        "fallbacks": [
            {
                "agent": fallback_agent,
                "provider": fallback_provider,
                "model": fallback_model,
            }
        ],
        "max_fallback_attempts": 1,
        "max_same_provider_retries": max_same_provider_retries,
        "cooldown_seconds": 600,
        "circuit_breaker": {
            "failure_threshold": 2,
            "cooldown_seconds": 900,
        },
    }


async def _configure_provider_monitor_workspace(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    agent: str = "gemini",
    model: str = "gemini-2.5-pro",
    fallback_agent: str = "codex",
    fallback_provider: str = "openai",
    fallback_model: str = "gpt-5.3-codex",
    max_same_provider_retries: int = 1,
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.agent = agent
        workspace.auto_merge = False
        workspace.initial_review_grace_period_seconds = 75
        workspace.task_policy = {
            "agent_model": model,
            "provider_recovery": _provider_recovery_policy(
                fallback_agent=fallback_agent,
                fallback_provider=fallback_provider,
                fallback_model=fallback_model,
                max_same_provider_retries=max_same_provider_retries,
            ),
            "pr_monitor": {"review_grace_seconds": 75},
        }
        await session.commit()


async def _provider_recovery_snapshot(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> tuple[dict[str, object], list[dict[str, object]], list[Operation], list[str]]:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        source_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.provider_recovery_requested"
        ]
        operations = list((await session.execute(select(Operation))).scalars())
        requested_ids = list(
            (
                await session.execute(
                    select(Workspace.id).where(Workspace.status == WorkspaceStatus.requested.value)
                )
            ).scalars()
        )
        return (
            dict(workspace.task_policy),
            [dict(event.payload or {}) for event in source_events],
            operations,
            requested_ids,
        )


async def _mark_refactor_task(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    auto_merge: bool = True,
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.task_class = TaskClass.refactor_task.value
        workspace.auto_merge = auto_merge
        await session.commit()


async def _dispatch_merge_recovery(
    *,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    workspace_id: str,
    pr_number: int,
    head_sha: str,
    sleep_fn: RecordedSleep | None = None,
) -> bool:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn or RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    return await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )


@pytest.mark.unit
async def test_monitor_recovery_dispatch_records_operation_with_pr_and_sha_context(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 77
    head_sha = "d" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)

    terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )

    assert terminal is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
        recovery_events = [
            event for event in workspace.events if event.event_type == "monitor.recovery_dispatched"
        ]
        state_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.state_changed"
            and event.reason_code == "RECOVERY_DISPATCH"
        ]
    assert workspace.status == WorkspaceStatus.ready.value
    assert len(operations) == 1
    operation = operations[0]
    assert operation.type == "validate"
    assert operation.status == OperationStatus.pending.value
    assert operation.idempotency_key is not None
    assert operation.idempotency_key.startswith("pr_monitor:validate_only:")
    assert len(operation.idempotency_key) <= 128
    assert operation.payload == {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": "validate_only",
        "requested_action": "validate",
        "reason": "Required validation tier has not passed for this merge candidate.",
        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        "stale_reason": "validation_insufficient_tier",
        "recovery_mode": "validate_only",
        "pr_number": pr_number,
        "pr_url": f"https://github.com/dimileeh/aira-web/pull/{pr_number}",
        "source_head_sha": head_sha,
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
    }
    assert len(recovery_events) == 1
    assert recovery_events[0].reason_code == "RECOVERY_DISPATCH"
    assert recovery_events[0].payload == {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "reason": "validation_insufficient_tier",
        "req_action": "validate",
        "recovery_mode": "validate_only",
    }
    assert len(state_events) == 1
    assert state_events[0].old_state == WorkspaceStatus.monitoring_pr.value
    assert state_events[0].new_state == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_monitor_recovery_dispatch_preserves_planning_validation_handoff_context(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 78
    head_sha = "e" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)
    plan_path = f"docs/awf-plans/{workspace_id}.md"
    report_path = f"docs/awf-plans/{workspace_id}.conformance.json"
    async with factory() as session:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        await workspace_repo.add_event(
            workspace,
            event_type="workspace.planning_conformance_requires_awf_validation",
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
            payload={
                "summary": "AWF validation evidence is required before conformance can pass.",
                "gaps": ["AWF-owned validation evidence is missing for the pytest gate."],
                "report_reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "plan_path": plan_path,
                "report_path": report_path,
                "iteration": 1,
                "max_iterations": 3,
            },
        )
        await session.commit()

    terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert terminal is True
    assert len(operations) == 1
    assert operations[0].payload["conformance"] == {
        "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
        "report_reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
        "summary": "AWF validation evidence is required before conformance can pass.",
        "gaps": ["AWF-owned validation evidence is missing for the pytest gate."],
        "plan_path": plan_path,
        "report_path": report_path,
        "iteration": 1,
        "max_iterations": 3,
    }


@pytest.mark.unit
async def test_monitor_recovery_dispatch_omits_satisfied_planning_validation_handoff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 79
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)
    async with factory() as session:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        await workspace_repo.add_event(
            workspace,
            event_type="workspace.planning_conformance_requires_awf_validation",
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
            payload={
                "summary": "AWF validation evidence is required before conformance can pass.",
                "gaps": ["AWF-owned validation evidence is missing for the pytest gate."],
                "report_reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "plan_path": f"docs/awf-plans/{workspace_id}.md",
                "report_path": f"docs/awf-plans/{workspace_id}.conformance.json",
                "iteration": 0,
                "max_iterations": 3,
            },
        )
        await workspace_repo.add_event(
            workspace,
            event_type="workspace.post_validation_conformance_satisfied",
            reason_code="PLAN_CONFORMANCE_SATISFIED",
            payload={
                "summary": "validation evidence satisfied the plan",
                "plan_path": f"docs/awf-plans/{workspace_id}.md",
                "report_path": f"docs/awf-plans/{workspace_id}.conformance.json",
                "validation_run_id": "val-resolved",
            },
        )
        await session.commit()

    terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert terminal is True
    assert len(operations) == 1
    assert "conformance" not in operations[0].payload


@pytest.mark.unit
async def test_monitor_runner_loads_persisted_state_on_resume(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 91
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha="f" * 40,
    )
    monitor_started_at = datetime.now(UTC) - timedelta(minutes=12)
    review_started_at = datetime.now(UTC) - timedelta(minutes=7)
    grace_started_key = _initial_review_grace_started_key(pr_number)
    persisted_threads = {
        "thread-1": "fix_committed",
        "thread-2": "defer",
        grace_started_key: _initial_review_grace_wall_started_value_from_datetime(
            review_started_at
        ),
    }
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_iter_count = 8
        workspace.monitor_threads_addressed = dict(persisted_threads)
        workspace.monitor_last_commit_sha = "e" * 40
        workspace.monitor_started_at = monitor_started_at
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    before = time.monotonic()
    workspace = await runner._load_workspace(workspace_id)
    state = runner._load_state(workspace)
    after = time.monotonic()

    assert state.iter_count == 8
    assert state.last_push_sha == "e" * 40
    assert state.threads_addressed_ids["thread-1"] == "fix_committed"
    assert state.threads_addressed_ids["thread-2"] == "defer"

    monitor_elapsed = (datetime.now(UTC) - monitor_started_at).total_seconds()
    assert before - monitor_elapsed - 1 <= state.started_at <= after - monitor_elapsed + 1

    grace_elapsed = (datetime.now(UTC) - review_started_at).total_seconds()
    grace_runtime_started = float(state.threads_addressed_ids[grace_started_key])
    assert before - grace_elapsed - 1 <= grace_runtime_started <= after - grace_elapsed + 1


@pytest.mark.unit
async def test_validation_recovery_dispatch_is_idempotent_for_duplicate_tick_replay(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 78
    head_sha = "e" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)

    first_terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    replay_sleep = RecordedSleep()
    replay_terminal = await _dispatch_merge_recovery(
        factory=factory,
        tmp_path=tmp_path,
        workspace_id=workspace_id,
        pr_number=pr_number,
        head_sha=head_sha,
        sleep_fn=replay_sleep,
    )

    assert first_terminal is True
    assert replay_terminal is False
    assert replay_sleep.calls == [60]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
        recovery_events = [
            event for event in workspace.events if event.event_type == "monitor.recovery_dispatched"
        ]
    recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
    wait_operations = [
        op
        for op in operations
        if op.type == OperationType.monitor_state.value
        and op.payload.get("reason_code") == "RECOVERY_IN_PROGRESS"
    ]
    assert len(recovery_operations) == 1
    assert len(wait_operations) == 1
    assert recovery_operations[0].idempotency_key is not None
    assert recovery_operations[0].idempotency_key.startswith("pr_monitor:validate_only:")
    assert len(recovery_operations[0].idempotency_key) <= 128
    assert wait_operations[0].status == OperationStatus.succeeded.value
    assert wait_operations[0].payload["action"] == "recovery_wait"
    assert wait_operations[0].payload["requested_action"] == "validate"
    assert wait_operations[0].payload["wait_seconds"] == 60
    assert wait_operations[0].payload["recovery_mode"] == "validate_only"
    assert wait_operations[0].payload["stale_reason"] == "validation_insufficient_tier"
    assert wait_operations[0].result == {
        "status": "succeeded",
        "outcome": "wait_elapsed",
        "slept_seconds": 60,
    }
    assert len(recovery_events) == 1


@pytest.mark.unit
async def test_late_validation_recovery_callback_records_stale_ready_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    pr_number = 79
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)
    async with factory() as session:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        await workspace_repo.transition(
            workspace,
            to=WorkspaceStatus.ready,
            reason_code="TEST_READY_AFTER_RECOVERY_DISPATCH",
        )
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

    terminal = await runner._handle_merge_gate_blocker(
        gate=_MergeGateResult(
            workspace=workspace,
            stale_reason="validation_insufficient_tier",
            req_action="validate",
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        stale_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.stale_callback_ignored"
        ]
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert len(stale_events) == 1
    assert stale_events[0].reason_code == "STALE_CALLBACK_IGNORED"
    assert stale_events[0].payload["callback_action"] == "recovery_dispatch"
    assert stale_events[0].payload["actual_status"] == WorkspaceStatus.ready.value
    assert [op for op in operations if op.type == OperationType.validate.value] == []


@pytest.mark.unit
async def test_review_comment_provider_failure_records_retry_and_ignores_comment(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        max_same_provider_retries=1,
    )

    mocker.patch(
        "awf.runtime.pr_monitor_runner.provider_ops.create_provider_recovery_attempt_row",
        return_value=None,
    )

    adapter = FakeAdapter()
    adapter.queue(
        returncode=1,
        stderr="Gemini RESOURCE_EXHAUSTED: provider is temporarily overloaded",
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)

    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    c = ReviewComment(
        comment_id="C_provider",
        body_excerpt="please fix",
        author="bot",
    )
    status = _status(reviews=(c,))
    state = MonitorState(started_at=0.0)

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._execute(
            action=AddressComments(threads=(), review_comments=(c,)),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=status,
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    assert "C_provider" not in state.threads_addressed_ids


@pytest.mark.unit
async def test_run_returns_after_terminal_agent_service_recovery_sentinel(
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

    async def _fetch_status_for_decision(**_kwargs: object) -> PRStatus:
        return _green_status()

    async def _refresh_pr_feedback_resolution_state(**_kwargs: object) -> bool:
        return False

    async def _resolve_addressed_outdated_threads(**_kwargs: object) -> None:
        return None

    async def _execute(**kwargs: object) -> bool:
        await runner._terminate_failed(
            str(kwargs["workspace_id"]),
            message="monitor: agent service unhealthy after restart attempts",
            reason_code=AGENT_SERVICE_UNHEALTHY,
        )
        raise _MonitorAgentServiceRecoveryFailedError("agent service unhealthy")

    runner._fetch_status_for_decision = _fetch_status_for_decision  # type: ignore[method-assign]
    runner._refresh_pr_feedback_resolution_state = (  # type: ignore[method-assign]
        _refresh_pr_feedback_resolution_state
    )
    runner._resolve_addressed_outdated_threads = (  # type: ignore[method-assign]
        _resolve_addressed_outdated_threads
    )
    runner._execute = _execute  # type: ignore[method-assign]

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        unexpected_recovery_failures = [
            event for event in workspace.events if event.reason_code == "MONITOR_RECOVERY_FAILED"
        ]
        unhealthy_events = [
            event for event in workspace.events if event.reason_code == AGENT_SERVICE_UNHEALTHY
        ]

    assert workspace.status == WorkspaceStatus.failed.value
    assert len(unhealthy_events) == 1
    assert unexpected_recovery_failures == []


@pytest.mark.unit
async def test_monitor_agent_idle_timeout_restarts_service_and_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={
                "provider_recovery": {
                    "provider": "google",
                    "model": "gemini-2.5-pro",
                }
            },
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: restarted")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    probe = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    command_evidence: list[str] = []
    compose_file = tmp_path / "compose.yml"

    result = await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=compose_file,
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=command_evidence,
    )

    assert result.stdout == "AWF-VERDICT: FIXED: restarted"
    assert adapter.calls == ["fix the comment", "fix the comment"]
    assert command_evidence == [
        "monitor idle timeout while agent service was down",
        "AWF-VERDICT: FIXED: restarted",
    ]
    probe.assert_awaited_once()
    ensure_project_up.assert_awaited_once_with(
        project_name="proj",
        compose_file=compose_file,
        workspace_id=workspace_id,
        wait=True,
        compose_up_timeout_seconds=300,
    )


@pytest.mark.unit
async def test_monitor_agent_idle_timeout_uses_workspace_compose_timeout_for_restart(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.resolved_profile = WorkspaceProfile(
            name="monitor-restart-timeout",
            docker=ProfileDocker(startup_timeout_seconds=420),
        ).model_dump(mode="json")
        workspace.task_policy = {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:backend.git",
                    "base_branch": "main",
                    "compose_up_timeout_seconds": 900,
                }
            ]
        }
        await session.commit()

    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={
                "provider_recovery": {
                    "provider": "google",
                    "model": "gemini-2.5-pro",
                }
            },
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: restarted")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    compose_file = tmp_path / "compose.yml"

    result = await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=compose_file,
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=[],
    )

    assert result.stdout == "AWF-VERDICT: FIXED: restarted"
    ensure_project_up.assert_awaited_once_with(
        project_name="proj",
        compose_file=compose_file,
        workspace_id=workspace_id,
        wait=True,
        compose_up_timeout_seconds=900,
    )


@pytest.mark.unit
async def test_monitor_agent_cleanup_service_down_restarts_service_and_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf-test-cleanup",
            source="agent",
            label="monitor",
            message='service "agent" is not running',
            cleanup_result=CommandResult(
                returncode=1,
                stdout="",
                stderr='service "agent" is not running',
            ),
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: cleanup restarted")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    probe = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    command_evidence: list[str] = []
    compose_file = tmp_path / "compose.yml"

    result = await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=compose_file,
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=command_evidence,
    )

    assert result.stdout == "AWF-VERDICT: FIXED: cleanup restarted"
    assert adapter.calls == ["fix the comment", "fix the comment"]
    assert command_evidence == [
        'service "agent" is not running',
        "AWF-VERDICT: FIXED: cleanup restarted",
    ]
    probe.assert_awaited_once()
    ensure_project_up.assert_awaited_once_with(
        project_name="proj",
        compose_file=compose_file,
        workspace_id=workspace_id,
        wait=True,
        compose_up_timeout_seconds=300,
    )


@pytest.mark.unit
async def test_monitor_agent_service_recovery_stops_when_workspace_leaves_monitoring(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: should not run")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )

    async def _cancel_workspace_after_restart(*_args: object, **_kwargs: object) -> None:
        async with factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            assert workspace is not None
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code="TEST_CANCELLED_DURING_RESTART",
            )
            await session.commit()

    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=_cancel_workspace_after_restart,
    )

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    assert adapter.calls == ["fix the comment"]
    ensure_project_up.assert_awaited_once()


@pytest.mark.unit
async def test_monitor_agent_service_recovery_stops_when_monitor_claim_is_superseded(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-old"
        await session.commit()

    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: should not run")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._monitor_owner_id = "worker-old"
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )

    async def _supersede_monitor_claim_after_restart(*_args: object, **_kwargs: object) -> None:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.monitor_claimed_by = "worker-new"
            await session.commit()

    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=_supersede_monitor_claim_after_restart,
    )

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    assert adapter.calls == ["fix the comment"]
    ensure_project_up.assert_awaited_once()


@pytest.mark.unit
async def test_monitor_agent_unrelated_cleanup_failure_is_not_recovered(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    cleanup_error = ComposeExecCleanupError(
        invocation_id="awf-test-cleanup",
        source="agent",
        label="monitor",
        message="permission denied",
        cleanup_result=CommandResult(
            returncode=1,
            stdout="",
            stderr="permission denied",
        ),
    )
    adapter = FakeAdapter()
    adapter.queue(exc=cleanup_error)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )

    with pytest.raises(ComposeExecCleanupError) as raised:
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id="ws_monitor_cleanup_passthrough",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    assert raised.value is cleanup_error
    ensure_project_up.assert_not_awaited()


@pytest.mark.unit
async def test_monitor_agent_service_restart_failure_terminates_without_provider_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=RuntimeError("compose unavailable"),
    )

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        event_types = [event.event_type for event in workspace.events]
        unhealthy_events = [
            event for event in workspace.events if event.reason_code == AGENT_SERVICE_UNHEALTHY
        ]

    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.failure_reason == "infrastructure_failure"
    assert "workspace.provider_recovery_requested" not in event_types
    assert len(unhealthy_events) == 1
    assert unhealthy_events[0].event_type == "workspace.state_changed"
    assert unhealthy_events[0].payload["details"]["agent_service_recovery"] == {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": AGENT_IDLE_TIMEOUT,
        "service_healthy": False,
        "restart_attempts": 1,
    }


@pytest.mark.unit
async def test_monitor_agent_service_recovery_exhaustion_terminates_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    for _ in range(3):
        adapter.queue(
            exc=AgentRunError(
                agent=AgentRuntime.claude_code,
                result=CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="monitor idle timeout while agent service stayed down",
                ),
                reason_code=AGENT_IDLE_TIMEOUT,
                details={"provider": "google", "model": "gemini-2.5-pro"},
            )
        )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        unhealthy_events = [
            event for event in workspace.events if event.reason_code == AGENT_SERVICE_UNHEALTHY
        ]

    assert adapter.calls == ["fix the comment", "fix the comment", "fix the comment"]
    assert ensure_project_up.await_count == 2
    assert workspace.status == WorkspaceStatus.failed.value
    assert unhealthy_events[-1].payload["details"]["agent_service_recovery"] == {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": AGENT_IDLE_TIMEOUT,
        "service_healthy": False,
        "restart_attempts": 2,
    }


@pytest.mark.unit
async def test_comment_repair_idle_timeout_uses_in_place_monitor_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        max_same_provider_retries=0,
    )

    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while addressing comments",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    comment = ReviewComment(
        comment_id="C_idle_timeout",
        body_excerpt="please fix",
        author="review-bot",
    )
    state = MonitorState(started_at=0.0)

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._execute(
            action=AddressComments(threads=(), review_comments=(comment,)),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_status(reviews=(comment,)),
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    source_policy, recovery_events, operations, requested_ids = await _provider_recovery_snapshot(
        factory,
        workspace_id,
    )
    comment_ops = [operation for operation in operations if operation.type == "comment_repair"]

    assert "C_idle_timeout" not in state.threads_addressed_ids
    assert requested_ids == []
    assert source_policy["provider_recovery_state"]["action"] == "fallback"
    assert source_policy["provider_recovery_state"]["target_agent"] == "codex"
    assert len(recovery_events) == 1
    assert "new_workspace_id" not in recovery_events[0]
    assert recovery_events[0]["provider_recovery"]["decision_reason_code"] == (
        "PROVIDER_FALLBACK_SELECTED"
    )
    assert len(comment_ops) == 1
    assert comment_ops[0].status == OperationStatus.failed.value
    assert comment_ops[0].result["outcome"] == "provider_retry"


@pytest.mark.unit
async def test_provider_failure_stale_callback_is_deterministic(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(factory, workspace_id)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(
            workspace,
            to=WorkspaceStatus.completed,
            reason_code="TEST_COMPLETED",
        )
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    exc = AgentRunError(
        agent=AgentRuntime.claude_code,
        result=CommandResult(
            returncode=1,
            stdout="",
            stderr="Gemini RESOURCE_EXHAUSTED: provider is temporarily overloaded",
        ),
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        details={"provider": "google", "model": "gemini-2.5-pro"},
    )

    action = await runner._record_provider_agent_run_error(workspace_id, exc)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        event_types = [event.event_type for event in workspace.events]

    assert action == "deterministic"
    assert "workspace.stale_callback_ignored" in event_types
    assert "workspace.provider_recovery_requested" not in event_types


@pytest.mark.unit
async def test_review_comment_deterministic_failure_is_marked_addressed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        returncode=1,
        stderr="Syntax error: invalid character",
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)

    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    c = ReviewComment(
        comment_id="C_deterministic",
        body_excerpt="please fix syntax",
        author="bot",
    )
    status = _status(reviews=(c,))
    state = MonitorState(started_at=0.0)

    terminal = await runner._execute(
        action=AddressComments(threads=(), review_comments=(c,)),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert "C_deterministic" in state.threads_addressed_ids
    assert state.threads_addressed_ids["C_deterministic"] == "agent_failed"
