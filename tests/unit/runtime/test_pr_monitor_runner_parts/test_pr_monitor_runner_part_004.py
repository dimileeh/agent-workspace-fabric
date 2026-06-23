"""Unit tests for focused ``pr_monitor_runner`` behavior.

Most cases cover the pure, side-effect-free helpers: ``_parse_verdict`` (CLI
reply → structured verdict) and ``_collect_defer_items`` (PRStatus +
MonitorState → bot/human defer buckets for the terminal artifact). Focused
runtime-path regressions live here when the unit suite needs to cover a
specific merge-gate branch without running the full monitor integration loop.
"""

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
    CheckFailure,
    CheckState,
    CheckTiming,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReportCiFailure,
    ReviewComment,
    ReviewThread,
    SyncBase,
)
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _as_utc,
    _collect_defer_items,
    _is_pending_check,
    _monitor_state_verdict,
    _parse_verdict,
    _parse_verdict_result,
    _stale_pending_check_warning_key,
    _stale_pending_check_warnings,
    _with_ci_failures,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
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
    ) -> tuple[CheckFailure, ...]:
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


class TestParseVerdict:
    @pytest.mark.unit
    def test_empty_stdout_needs_human(self) -> None:
        # #305: empty agent output is a failure to produce, not a considered
        # defer. Block the merge (needs_human) rather than auto-capturing a
        # follow-up tracking issue on a thread the agent never addressed.
        assert _parse_verdict("") == "needs_human"

    @pytest.mark.unit
    def test_false_positive_marker(self) -> None:
        assert _parse_verdict("FALSE POSITIVE: reviewer misread the diff") == "false_positive"

    @pytest.mark.unit
    def test_private_awf_verdict_false_positive_marker(self) -> None:
        assert (
            _parse_verdict("AWF-VERDICT: FALSE POSITIVE: stale review boilerplate")
            == "false_positive"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_marker_preserves_reason(self) -> None:
        # #305: NEEDS_HUMAN maps to its own needs_human verdict (blocks merge,
        # never auto-resolved), distinct from a follow-up defer.
        result = _parse_verdict_result("AWF-VERDICT: NEEDS_HUMAN: maintainer decision")

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision"

    @pytest.mark.unit
    def test_private_awf_verdict_uses_final_line_not_prompt_echo(self) -> None:
        stdout = (
            'Re-reading: "print AWF-VERDICT: NEEDS_HUMAN: <what you need> and exit."\n'
            "Some deliberation about the tradeoff.\n"
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must choose the checkout policy"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must choose the checkout policy"

    @pytest.mark.unit
    def test_private_awf_and_bare_mixed_verdict_uses_later_bare_match(self) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: DEFER: follow this later\nNEEDS_HUMAN: merge needs maintainer decision"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "merge needs maintainer decision"

    @pytest.mark.unit
    def test_private_awf_multiple_needs_human_uses_latest_reason(self) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: first pass needs human review\nAWF-VERDICT: NEEDS_HUMAN:"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "first pass needs human review"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_inline_prompt_template(self) -> None:
        stdout = (
            'Re-reading: "If you need a human decision, print '
            'AWF-VERDICT: NEEDS_HUMAN: <what you need> and exit."'
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_placeholder_only_needs_human_has_no_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: NEEDS_HUMAN: <what you need>")

        assert result.verdict == "needs_human"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_without_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: NEEDS_HUMAN:")

        assert result.verdict == "needs_human"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_defer_without_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: DEFER:")

        assert result.verdict == "defer"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_space_variant_preserves_reason(self) -> None:
        # The primary _AWF_VERDICT regex tolerates "NEEDS HUMAN" (space) like
        # "FALSE POSITIVE", so the reason is extracted cleanly instead of being
        # garbled by the bare fallback (which splits on the AWF-VERDICT colon).
        result = _parse_verdict_result("AWF-VERDICT: NEEDS HUMAN: maintainer decision")

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "label",
        [
            "NEEDS_HUMAN",
            "NEEDS HUMAN",
            "NEEDS_ HUMAN",
            "NEEDS _HUMAN",
            "NEEDS__HUMAN",
            "needs_human",
        ],
    )
    def test_private_awf_verdict_needs_human_separator_variants(self, label: str) -> None:
        # Any separator the NEEDS[\s_]+HUMAN regex accepts must normalize to
        # needs_human — never silently fall through to fix_committed (#305).
        result = _parse_verdict_result(f"AWF-VERDICT: {label}: maintainer decision")

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision"

    @pytest.mark.unit
    def test_private_awf_verdict_defer_placeholder_only_has_no_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: DEFER: <defer follow-up needed>")

        assert result.verdict == "defer"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_marker_preserves_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: FIXED: pushed regression test")

        assert result.verdict == "fix_committed"
        assert result.reason == "pushed regression test"

    @pytest.mark.unit
    def test_false_positive_case_insensitive(self) -> None:
        assert _parse_verdict("false positive: minor") == "false_positive"

    @pytest.mark.unit
    def test_defer_marker(self) -> None:
        assert _parse_verdict("DEFER: needs human judgement") == "defer"

    @pytest.mark.unit
    def test_plain_reply_counts_as_fix_committed(self) -> None:
        assert _parse_verdict("Committed fix in abc1234: renamed variable.") == "fix_committed"

    @pytest.mark.unit
    def test_later_defer_does_not_overwrite_prior_false_positive_marker(self) -> None:
        # Hardening keeps blocking verdicts from being demoted by a later defer.
        reply = "FALSE POSITIVE: not a real issue.\nDEFER: follow-up issue"
        assert _parse_verdict(reply) == "false_positive"

    @pytest.mark.unit
    def test_later_defer_does_not_overwrite_bare_needs_human(self) -> None:
        # ``NEEDS_HUMAN`` must keep merge-blocking priority over later defer text.
        reply = "NEEDS_HUMAN: follow-up needed\nDEFER: follow-up issue"
        assert _parse_verdict(reply) == "needs_human"

    @pytest.mark.unit
    def test_bare_false_positive_takes_precedence_over_bare_defer(self) -> None:
        reply = "DEFER: fix this later\nFALSE POSITIVE: not a real problem"
        assert _parse_verdict(reply) == "false_positive"

    @pytest.mark.unit
    def test_monitor_state_verdict_normalizes_persisted_private_verdicts(self) -> None:
        # #305: needs_human is now its own verdict, no longer collapsed to defer.
        assert _monitor_state_verdict("NEEDS_HUMAN") == "needs_human"
        assert _monitor_state_verdict("defer") == "defer"
        assert _monitor_state_verdict("agent_failed") == "agent_failed"
        assert _monitor_state_verdict("fixed") == "fix_committed"


class TestCollectDeferItems:
    @pytest.mark.unit
    def test_empty_status_yields_empty_buckets(self) -> None:
        bots, humans = _collect_defer_items(_status(), MonitorState())
        assert bots == []
        assert humans == []

    @pytest.mark.unit
    def test_thread_deferred_by_bot_goes_to_bot_bucket(self) -> None:
        t = ReviewThread(
            thread_id="T1",
            path="src/x.py",
            line=1,
            body_excerpt="nit",
            author="reviewer-bot[bot]",
        )
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        bots, humans = _collect_defer_items(_status(inline=(t,)), state)
        assert len(bots) == 1
        assert bots[0]["id"] == "T1"
        assert bots[0]["kind"] == "thread"
        assert humans == []

    @pytest.mark.unit
    def test_thread_deferred_by_human_goes_to_human_bucket(self) -> None:
        t = ReviewThread(
            thread_id="T2",
            path="src/y.py",
            line=5,
            body_excerpt="real concern",
            author="dimileeh",
        )
        state = MonitorState(threads_addressed_ids={"T2": "defer"})
        bots, humans = _collect_defer_items(_status(inline=(t,)), state)
        assert bots == []
        assert len(humans) == 1
        assert humans[0]["id"] == "T2"

    @pytest.mark.unit
    def test_non_deferred_items_are_excluded(self) -> None:
        t = ReviewThread(
            thread_id="T3",
            path=None,
            line=None,
            body_excerpt="fixed",
            author="reviewer-bot[bot]",
        )
        state = MonitorState(threads_addressed_ids={"T3": "fix_committed"})
        bots, humans = _collect_defer_items(_status(inline=(t,)), state)
        assert bots == []
        assert humans == []

    @pytest.mark.unit
    def test_non_deferred_review_comments_are_excluded(self) -> None:
        c = ReviewComment(
            comment_id="C2",
            body_excerpt="already handled",
            author="dimileeh",
        )

        bots, humans = _collect_defer_items(_status(reviews=(c,)), MonitorState())

        assert bots == []
        assert humans == []

    @pytest.mark.unit
    def test_review_comment_deferred_includes_kind_review(self) -> None:
        c = ReviewComment(
            comment_id="C1",
            body_excerpt="overall concern",
            author="greptile-apps[bot]",
        )
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        bots, humans = _collect_defer_items(_status(reviews=(c,)), state)
        assert len(bots) == 1
        assert bots[0]["kind"] == "review"
        assert bots[0]["id"] == "C1"
        assert humans == []


class TestRunnerConfigShape:
    @pytest.mark.unit
    def test_runner_config_defaults_include_safety_net(self) -> None:
        """The runner keeps ``max_outer_iterations`` as a pure safety net
        against decision-loop bugs — a legitimate session exits via a
        terminal action well before this. The cap that WAS removed is
        ``MonitorConfig.iter_cap`` (decision-core gate). Keep these
        distinct so future refactors don't conflate them."""
        cfg = MonitorRunnerConfig()
        assert cfg.max_outer_iterations >= 1000
        assert cfg.max_fix_cycle_passes >= 1


class TestPendingCheckHelpers:
    @pytest.mark.unit
    def test_pending_check_warnings_include_only_old_non_terminal_checks(self) -> None:
        now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
        old = now - timedelta(minutes=10)
        status = replace(
            _status(),
            checks=(
                CheckTiming(
                    name="ci/build",
                    status="IN_PROGRESS",
                    started_at=old,
                    details_url="https://checks.example/build",
                ),
                CheckTiming(name="ci/no-start", status="PENDING", started_at=None),
                CheckTiming(name="ci/fresh", status="QUEUED", started_at=now),
                CheckTiming(name="ci/done", status="COMPLETED", conclusion=None, started_at=old),
                CheckTiming(name="ci/skipped", status=None, conclusion="SKIPPED", started_at=old),
            ),
        )

        disabled = _stale_pending_check_warnings(
            status,
            now=now,
            threshold_seconds=0,
        )
        warnings = _stale_pending_check_warnings(
            status,
            now=now,
            threshold_seconds=120,
        )

        assert disabled == ()
        assert len(warnings) == 1
        assert warnings[0].payload() == {
            "check_name": "ci/build",
            "age_seconds": 600,
            "head_sha": "abc123",
            "pr_number": 42,
            "threshold_seconds": 120,
            "threshold_window": 5,
            "check_status": "IN_PROGRESS",
            "check_conclusion": None,
            "details_url": "https://checks.example/build",
        }
        assert (
            _stale_pending_check_warning_key(
                workspace_id="ws_1",
                head_sha="abc123",
                check_name="ci/build",
                threshold_seconds=120,
                threshold_window=5,
            )
            == '__awf_pending_check_stale__:["ws_1","abc123","ci/build","120",5]'
        )

    @pytest.mark.unit
    def test_pending_check_classifier_handles_provider_status_edges(self) -> None:
        assert _is_pending_check(CheckTiming(name="unknown", status="waiting")) is True
        assert _is_pending_check(CheckTiming(name="terminal", status="success")) is False
        assert (
            _is_pending_check(CheckTiming(name="terminal-conclusion", conclusion="timed_out"))
            is False
        )
        assert _is_pending_check(CheckTiming(name="future-provider", status="mystery")) is True
        assert _is_pending_check(CheckTiming(name="empty")) is False
        naive = datetime(2026, 4, 27, 12, 0)
        assert _as_utc(naive).tzinfo is UTC
