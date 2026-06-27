"""Unit tests for focused ``pr_monitor_runner`` behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_mock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import (
    AgentRuntime,
    OperationStatus,
    TaskClass,
    WorkspaceStatus,
)
from awf.db.models import Operation, Workspace
from awf.db.repositories import (
    OperationRepository,
    ProviderModelCircuitBreakerRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
    AddressOperatorHint,
    CheckFailure,
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
    ReportCiFailure,
    ReviewThread,
    SyncBase,
)
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _with_ci_failures,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor import _status


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


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
async def test_execute_sync_base_records_no_progress_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # fetch
    cmd.queue_result(returncode=0)  # merge
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    sync_base_retry_key = "__awf_base_fetch_retry_count:sync_base"
    state = MonitorState(threads_addressed_ids={sync_base_retry_key: "2"})

    terminal = await runner._execute(
        action=SyncBase(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(
            head_sha="abc1234567890def",
            mergeable=MergeableState.CONFLICTING,
            merge_state_status=MergeStateStatus.DIRTY,
        ),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.sync_base_no_progress_signature == (
        "abc1234567890def|CONFLICTING|DIRTY|base_behind=0"
    )
    assert state.sync_base_no_progress_count == 1
    assert sync_base_retry_key not in state.threads_addressed_ids


@pytest.mark.unit
async def test_execute_sync_base_failed_push_resets_no_progress_streak(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # fetch
    cmd.queue_result(returncode=0)  # merge
    cmd.queue_result(returncode=1, stderr="push rejected")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState(
        sync_base_no_progress_signature=("abc1234567890def|CONFLICTING|DIRTY|base_behind=0"),
        sync_base_no_progress_count=2,
    )

    terminal = await runner._execute(
        action=SyncBase(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(
            head_sha="abc1234567890def",
            mergeable=MergeableState.CONFLICTING,
            merge_state_status=MergeStateStatus.DIRTY,
        ),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.sync_base_no_progress_signature is None
    assert state.sync_base_no_progress_count == 0


@pytest.mark.unit
async def test_sync_base_progress_increments_same_snapshot_and_resets_on_failure(
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
    status = _status(
        head_sha="abc1234567890def",
        mergeable=MergeableState.CONFLICTING,
        merge_state_status=MergeStateStatus.DIRTY,
    )
    state = MonitorState(
        sync_base_no_progress_signature=("abc1234567890def|CONFLICTING|DIRTY|base_behind=0"),
        sync_base_no_progress_count=1,
    )

    runner._record_sync_base_progress(
        state=state,
        status=status,
        push_result=_GitPushResult(pushed=False, failed=False, returncode=0),
    )
    assert state.sync_base_no_progress_count == 2

    runner._record_sync_base_progress(
        state=state,
        status=status,
        push_result=_GitPushResult(pushed=False, failed=True, returncode=128),
    )
    assert state.sync_base_no_progress_signature is None
    assert state.sync_base_no_progress_count == 0


@pytest.mark.unit
async def test_load_state_ignores_invalid_persisted_no_progress_count(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = {
            "__awf_sync_base_no_progress_signature": "sig",
            "__awf_sync_base_no_progress_count": "not-an-int",
            "T1": "defer",
        }
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    workspace = await runner._load_workspace(workspace_id)
    state = runner._load_state(workspace)

    assert state.sync_base_no_progress_signature == "sig"
    assert state.sync_base_no_progress_count == 0
    assert state.threads_addressed_ids == {"T1": "defer"}


@pytest.mark.unit
async def test_execute_sync_base_base_fetch_failure_finishes_operation_and_fails_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_base_fetch_error(**_kwargs: object) -> object:
        raise BaseFetchError("broken mirror")

    mocker.patch.object(runner, "_run_sync_base", _raise_base_fetch_error)

    terminal = await runner._execute(
        action=SyncBase(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(merge_state_status=MergeStateStatus.DIRTY),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert terminal is True
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.failure_message is not None
    assert "broken mirror" in workspace.failure_message
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "GIT_FETCH_BASE_FAILED"


@pytest.mark.unit
async def test_execute_sync_base_transient_exhaustion_records_terminal_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    object.__setattr__(runner._runner_config, "transient_base_fetch_max_retries", 0)

    async def _raise_transient_base_fetch_error(**_kwargs: object) -> object:
        raise BaseFetchError("git fetch base failed: HTTP 500 server error")

    mocker.patch.object(runner, "_run_sync_base", _raise_transient_base_fetch_error)

    terminal = await runner._execute(
        action=SyncBase(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(merge_state_status=MergeStateStatus.DIRTY),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert terminal is True
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.failed.value
    assert operations[0].status == OperationStatus.failed.value
    assert operations[0].error_code == "GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED"
    assert operations[0].result["reason_code"] == ("GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED")


@pytest.mark.unit
async def test_provider_circuit_breaker_suppresses_monitor_cli_and_records_event_and_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        agent="codex",
        model="gpt-5.3-codex",
    )
    async with factory() as session:
        await ProviderModelCircuitBreakerRepository(session).record_failure(
            provider="openai",
            model="gpt-5.3-codex",
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            failure_fingerprint="capacity:openai:gpt-5.3-codex",
            workspace_id=workspace_id,
            attempt_id=None,
            now=datetime.now(UTC),
            failure_threshold=1,
            cooldown_seconds=900,
        )
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    suppressed = await runner._provider_recovery_suppresses_cli(workspace_id)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        task_policy = workspace.task_policy
        events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.provider_recovery_cooldown"
        ]

    assert suppressed is True
    assert len(events) == 1
    assert events[0].reason_code == "PROVIDER_MODEL_CIRCUIT_OPEN"
    assert events[0].payload["provider"] == "openai"
    assert events[0].payload["model"] == "gpt-5.3-codex"
    assert events[0].payload["source"] == "pr_monitor"
    assert events[0].payload["failure_count"] == 1
    assert events[0].payload["last_reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
    recovery_state = task_policy["provider_recovery_state"]
    assert recovery_state["action"] == "retry"
    assert recovery_state["decision_reason_code"] == "PROVIDER_MODEL_CIRCUIT_OPEN"
    assert recovery_state["source_provider"] == "openai"
    assert recovery_state["source_model"] == "gpt-5.3-codex"
    assert recovery_state["source_reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
    assert isinstance(recovery_state["not_before"], str)


@pytest.mark.unit
async def test_provider_circuit_breaker_suppression_with_no_cooldown_uses_dedup_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        agent="codex",
        model="gpt-5.3-codex",
    )
    async with factory() as session:
        repo = ProviderModelCircuitBreakerRepository(session)
        breaker = await repo.record_failure(
            provider="openai",
            model="gpt-5.3-codex",
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            failure_fingerprint="capacity:openai:gpt-5.3-codex",
            workspace_id=workspace_id,
            attempt_id=None,
            now=datetime.now(UTC),
            failure_threshold=1,
            cooldown_seconds=900,
        )
        breaker.cooldown_until = None
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    first_suppressed = await runner._provider_recovery_suppresses_cli(workspace_id)
    first_policy, _, _, _ = await _provider_recovery_snapshot(
        factory,
        workspace_id,
    )
    second_suppressed = await runner._provider_recovery_suppresses_cli(workspace_id)

    source_policy, _, _, _ = await _provider_recovery_snapshot(
        factory,
        workspace_id,
    )
    recovery_state = source_policy["provider_recovery_state"]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        cooldown_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.provider_recovery_cooldown"
        ]

    assert first_suppressed is True
    assert second_suppressed is True
    assert (
        first_policy["provider_recovery_state"]["not_before"]
        == source_policy["provider_recovery_state"]["not_before"]
    )
    assert recovery_state["action"] == "retry"
    assert recovery_state["source_reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
    assert isinstance(recovery_state["not_before"], str)
    assert len(cooldown_events) == 1


@pytest.mark.unit
async def test_provider_recovery_suppresses_cli_refreshes_stale_task_policy_from_live_breaker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Validate stale monitor cooldown state is refreshed from updated breaker state."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        agent="codex",
        model="gpt-5.3-codex",
    )
    async with factory() as session:
        await ProviderModelCircuitBreakerRepository(session).record_failure(
            provider="openai",
            model="gpt-5.3-codex",
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            failure_fingerprint="capacity:openai:gpt-5.3-codex",
            workspace_id=workspace_id,
            attempt_id=None,
            now=datetime.now(UTC),
            failure_threshold=1,
            cooldown_seconds=600,
        )
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    first_suppressed = await runner._provider_recovery_suppresses_cli(workspace_id)
    assert first_suppressed is True

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        provider_recovery_state = workspace.task_policy["provider_recovery_state"]
        stale_not_before = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        workspace.task_policy = {
            **workspace.task_policy,
            "provider_recovery_state": {
                **provider_recovery_state,
                "not_before": stale_not_before,
            },
        }

        breaker_repo = ProviderModelCircuitBreakerRepository(session)
        await breaker_repo.record_failure(
            provider="openai",
            model="gpt-5.3-codex",
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            failure_fingerprint="capacity:openai:gpt-5.3-codex",
            workspace_id=workspace_id,
            attempt_id=None,
            now=datetime.now(UTC),
            failure_threshold=1,
            cooldown_seconds=1200,
        )
        await session.commit()

    second_suppressed = await runner._provider_recovery_suppresses_cli(workspace_id)

    source_policy, _, _, _ = await _provider_recovery_snapshot(
        factory,
        workspace_id,
    )
    recovery_state = source_policy["provider_recovery_state"]

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        cooldown_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.provider_recovery_cooldown"
        ]

    assert second_suppressed is True
    assert recovery_state["not_before"] != stale_not_before
    assert datetime.fromisoformat(recovery_state["not_before"]) > datetime.fromisoformat(
        stale_not_before
    )
    assert len(cooldown_events) == 1


@pytest.mark.unit
async def test_provider_agent_error_still_raises_full_fallback_for_non_monitor_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        max_same_provider_retries=0,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.provider_ops.create_provider_recovery_attempt_row",
        return_value=SimpleNamespace(action="fallback", in_place=False),
    )
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
            stderr="Gemini MODEL_CAPACITY_EXHAUSTED",
        ),
        details={"provider": "google", "model": "gemini-2.5-pro"},
    )

    with pytest.raises(ProviderRecoveryFallbackError):
        await runner._handle_provider_agent_run_error(workspace_id, exc)


@pytest.mark.unit
async def test_provider_agent_auth_failure_raises_provider_auth_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        agent="codex",
        model="gpt-5.5",
        fallback_agent="gemini",
        fallback_provider="google",
        fallback_model="gemini-3.1-pro-preview",
        max_same_provider_retries=3,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    exc = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(
            returncode=1,
            stdout="",
            stderr=(
                "Failed to refresh token: Your access token could not be refreshed "
                "because your refresh token was already used. websocket 401 Unauthorized "
                "token_expired"
            ),
        ),
        details={"provider": "openai", "model": "gpt-5.5"},
    )

    with pytest.raises(ProviderRecoveryAuthError):
        await runner._handle_provider_agent_run_error(workspace_id, exc)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        terminal_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.provider_recovery_terminal"
        ]

    assert len(terminal_events) == 1
    assert terminal_events[0].reason_code == "PROVIDER_AUTH_FAILED"
    assert workspace.task_policy["provider_recovery_state"]["action"] == "terminal"
    assert workspace.task_policy["provider_recovery_state"]["source_reason_code"] == (
        "AGENT_AUTH_FAILED"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    ["sync_base", "ci_repair", "comment_repair", "operator_hint_repair"],
)
async def test_agent_service_recovery_sentinel_finishes_monitor_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    case: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    status = _green_status()
    state = MonitorState(started_at=0.0)
    expected_result: dict[str, object] = {
        "status": "failed",
        "outcome": "agent_service_recovery_failed",
        "reason_code": "MONITOR_RECOVERY_FAILED",
        "pushed": False,
    }

    if case == "sync_base":
        action = SyncBase()
        target_method = "_run_sync_base"
        expected_type = "sync_base"
    elif case == "ci_repair":
        failures = (CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom"),)
        action = ReportCiFailure(failures=failures)
        status = _with_ci_failures(status, failures)
        target_method = "_run_ci_fix"
        expected_type = "ci_repair"
        expected_result["failure_count"] = 1
    elif case == "comment_repair":
        thread = ReviewThread(
            thread_id="T_service",
            path="src/app.py",
            line=12,
            body_excerpt="please fix",
            author="reviewer",
        )
        action = AddressComments(threads=(thread,), review_comments=())
        status = replace(status, unresolved_inline_threads=(thread,))
        target_method = "_run_fix_cycle"
        expected_type = "comment_repair"
        expected_result.update({"thread_count": 1, "review_comment_count": 0})
    else:
        hint = OperatorHint(
            reason="repair after operator guide",
            directive="fix it",
            operation_id="op_operator_hint",
            requested_at="2026-06-27T00:00:00+00:00",
            reason_code="OPERATOR_GUIDE",
        )
        action = AddressOperatorHint(hint=hint)
        state = MonitorState(started_at=0.0, pending_operator_hint=hint)
        target_method = "_run_operator_hint_cycle"
        expected_type = "comment_repair"

    async def _raise_agent_service_recovery_failed(**_kwargs: object) -> object:
        raise _MonitorAgentServiceRecoveryFailedError("agent service unhealthy")

    mocker.patch.object(runner, target_method, _raise_agent_service_recovery_failed)

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await runner._execute(
            action=action,
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

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == expected_type
    assert operation.status == OperationStatus.failed.value
    assert operation.result == expected_result
    assert operation.error_code == "MONITOR_RECOVERY_FAILED"
    assert operation.error_message == "agent service unhealthy"


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    ["sync_base", "ci_repair", "comment_repair", "operator_hint_repair"],
)
async def test_superseded_agent_service_recovery_cancels_monitor_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    case: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    status = _green_status()
    state = MonitorState(started_at=0.0)
    expected_result: dict[str, object] = {
        "status": "cancelled",
        "outcome": "agent_service_recovery_superseded",
        "reason_code": "MONITOR_RECOVERY_SUPERSEDED",
        "pushed": False,
    }

    if case == "sync_base":
        action = SyncBase()
        target_method = "_run_sync_base"
        expected_type = "sync_base"
    elif case == "ci_repair":
        failures = (CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom"),)
        action = ReportCiFailure(failures=failures)
        status = _with_ci_failures(status, failures)
        target_method = "_run_ci_fix"
        expected_type = "ci_repair"
        expected_result["failure_count"] = 1
    elif case == "comment_repair":
        thread = ReviewThread(
            thread_id="T_service",
            path="src/app.py",
            line=12,
            body_excerpt="please fix",
            author="reviewer",
        )
        action = AddressComments(threads=(thread,), review_comments=())
        status = replace(status, unresolved_inline_threads=(thread,))
        target_method = "_run_fix_cycle"
        expected_type = "comment_repair"
        expected_result.update({"thread_count": 1, "review_comment_count": 0})
    else:
        hint = OperatorHint(
            reason="repair after operator guide",
            directive="fix it",
            operation_id="op_operator_hint",
            requested_at="2026-06-27T00:00:00+00:00",
            reason_code="OPERATOR_GUIDE",
        )
        action = AddressOperatorHint(hint=hint)
        state = MonitorState(started_at=0.0, pending_operator_hint=hint)
        target_method = "_run_operator_hint_cycle"
        expected_type = "comment_repair"

    async def _raise_agent_service_recovery_superseded(**_kwargs: object) -> object:
        raise _MonitorAgentServiceRecoverySupersededError("agent service recovery superseded")

    mocker.patch.object(runner, target_method, _raise_agent_service_recovery_superseded)

    with pytest.raises(_MonitorAgentServiceRecoverySupersededError):
        await runner._execute(
            action=action,
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

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == expected_type
    assert operation.status == OperationStatus.cancelled.value
    assert operation.result == expected_result
    assert operation.error_code == "MONITOR_RECOVERY_SUPERSEDED"
    assert operation.error_message == "agent service recovery superseded"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_cls", "outcome", "reason_code"),
    [
        (ProviderRecoveryRetryError, "provider_retry", "PROVIDER_OUTAGE"),
        (ProviderRecoveryFallbackError, "provider_fallback", "PROVIDER_FALLBACK"),
        (ProviderRecoveryAuthError, "provider_auth_failed", "PROVIDER_AUTH_FAILED"),
    ],
)
async def test_sync_base_provider_recovery_exceptions_finish_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    error_cls: type[Exception],
    outcome: str,
    reason_code: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_provider_error(**_kwargs: object) -> object:
        raise error_cls()

    mocker.patch.object(runner, "_run_sync_base", _raise_provider_error)

    with pytest.raises(error_cls):
        await runner._execute(
            action=SyncBase(),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_green_status(),
            state=MonitorState(started_at=0.0),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == "sync_base"
    assert operation.status == OperationStatus.failed.value
    assert operation.result == {
        "status": "failed",
        "outcome": outcome,
        "reason_code": reason_code,
        "pushed": False,
    }
    assert operation.error_code == reason_code


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_cls", "outcome", "reason_code"),
    [
        (ProviderRecoveryRetryError, "provider_retry", "PROVIDER_OUTAGE"),
        (ProviderRecoveryFallbackError, "provider_fallback", "PROVIDER_FALLBACK"),
        (ProviderRecoveryAuthError, "provider_auth_failed", "PROVIDER_AUTH_FAILED"),
    ],
)
async def test_ci_repair_provider_recovery_exceptions_finish_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    error_cls: type[Exception],
    outcome: str,
    reason_code: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_provider_error(**_kwargs: object) -> object:
        raise error_cls()

    mocker.patch.object(runner, "_run_ci_fix", _raise_provider_error)
    failures = (CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom"),)

    with pytest.raises(error_cls):
        await runner._execute(
            action=ReportCiFailure(failures=failures),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_with_ci_failures(_green_status(), failures),
            state=MonitorState(started_at=0.0),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == "ci_repair"
    assert operation.status == OperationStatus.failed.value
    assert operation.result == {
        "status": "failed",
        "outcome": outcome,
        "reason_code": reason_code,
        "failure_count": 1,
        "pushed": False,
    }
    assert operation.error_code == reason_code


@pytest.mark.unit
async def test_comment_repair_provider_auth_exception_finishes_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_auth",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )

    async def _raise_provider_auth(**_kwargs: object) -> object:
        raise ProviderRecoveryAuthError()

    mocker.patch.object(runner, "_run_fix_cycle", _raise_provider_auth)

    with pytest.raises(ProviderRecoveryAuthError):
        await runner._execute(
            action=AddressComments(threads=(thread,), review_comments=()),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=replace(_green_status(), unresolved_inline_threads=(thread,)),
            state=MonitorState(started_at=0.0),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    operation = operations[0]
    assert operation.type == "comment_repair"
    assert operation.status == OperationStatus.failed.value
    assert operation.result == {
        "status": "failed",
        "outcome": "provider_auth_failed",
        "reason_code": "PROVIDER_AUTH_FAILED",
        "pushed": False,
    }
    assert operation.error_code == "PROVIDER_AUTH_FAILED"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_cls", "terminates"),
    [
        (ProviderRecoveryRetryError, False),
        (ProviderRecoveryFallbackError, True),
    ],
)
async def test_run_handles_provider_recovery_exceptions_without_crashing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    error_cls: type[Exception],
    terminates: bool,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    workspace_id = "ws_provider_recovery_run"
    state = MonitorState(started_at=0.0)
    workspace = SimpleNamespace(
        status=WorkspaceStatus.monitoring_pr.value,
        monitor_started_at=datetime.now(UTC),
        repo_url="git@github.com:dimileeh/aira-web.git",
        pr_number=42,
        branch_base="development",
        remote_push_branch="awf/ws_provider_recovery_run",
        task_kind="feature_branch_pr",
        branch_name="awf/ws_provider_recovery_run",
    )

    async def _raise_provider_error(**_kwargs: object) -> bool:
        raise error_cls()

    mocker.patch.object(runner, "_open_monitor_log", mocker.AsyncMock(return_value=None))
    write_log = mocker.patch.object(runner, "_write_monitor_log", mocker.AsyncMock())
    mocker.patch.object(runner, "_load_workspace", mocker.AsyncMock(return_value=workspace))
    mocker.patch.object(runner, "_load_state", return_value=state)
    mocker.patch.object(
        runner,
        "_fetch_status_for_decision",
        mocker.AsyncMock(return_value=_green_status()),
    )
    mocker.patch.object(runner, "_execute", _raise_provider_error)
    persist_state = mocker.patch.object(runner, "_persist_state", mocker.AsyncMock())
    terminate_failed = mocker.patch.object(
        runner,
        "_terminate_failed",
        mocker.AsyncMock(),
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    persist_state.assert_awaited_once_with(workspace_id, state)
    logged_events = [call.args[1]["event"] for call in write_log.await_args_list]
    if terminates:
        assert "monitor.provider_fallback" in logged_events
        terminate_failed.assert_awaited_once_with(
            workspace_id,
            message="monitor: provider recovery fallback triggered",
            reason_code="PROVIDER_FALLBACK",
        )
    else:
        assert "monitor.provider_retry" in logged_events
        terminate_failed.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_cls", "terminates"),
    [
        (ProviderRecoveryRetryError, False),
        (ProviderRecoveryFallbackError, True),
    ],
)
async def test_run_handles_provider_recovery_before_state_is_loaded(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    error_cls: type[Exception],
    terminates: bool,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    workspace_id = "ws_provider_recovery_early"
    mocker.patch.object(runner, "_open_monitor_log", mocker.AsyncMock(return_value=None))
    write_log = mocker.patch.object(runner, "_write_monitor_log", mocker.AsyncMock())
    mocker.patch.object(runner, "_load_workspace", mocker.AsyncMock(side_effect=error_cls()))
    persist_state = mocker.patch.object(runner, "_persist_state", mocker.AsyncMock())
    terminate_failed = mocker.patch.object(
        runner,
        "_terminate_failed",
        mocker.AsyncMock(),
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    persist_state.assert_not_awaited()
    logged_events = [call.args[1]["event"] for call in write_log.await_args_list]
    if terminates:
        assert "monitor.provider_fallback" in logged_events
        terminate_failed.assert_awaited_once_with(
            workspace_id,
            message="monitor: provider recovery fallback triggered",
            reason_code="PROVIDER_FALLBACK",
        )
    else:
        assert "monitor.provider_retry" in logged_events
        terminate_failed.assert_not_awaited()
