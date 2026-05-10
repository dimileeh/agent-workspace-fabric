"""Error-path coverage for ``awf.control.executor.WorkspaceExecutor``.

The happy/failure paths are covered in ``test_executor.py``. This
file targets specific error branches that need dedicated fixtures:

 - Constructor validation: pr_monitor + pr_monitor_factory can't both
   be set (line 107).
 - Unexpected exception during agent run (lines 166-174).
 - Missing base_commit on workspace (lines 192-202).
 - Commit step raises RuntimeError when git commit exits non-zero
   (line 227).
 - Unexpected exception wrapping the commit step (lines 318-326).
 - pr_monitor_factory path (line 501) — factory invoked with adapter.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.control.executor as executor_module
from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.commands import AsyncioSubprocessRunner, FakeCommandRunner
from awf.common.github_client import PullRequestAdoptionMetadata, RepoRef
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    _call_pr_monitor_factory,
    _required_metadata_str,
)
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.models import MergeCandidate, Operation, TaskAttempt, Workspace, WorkspaceEvent
from awf.db.repositories import (
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager, ComposeOperationError
from awf.profiles.models import ProfileMonitor, WorkspaceProfile
from awf.runtime.logs import LogStore
from awf.runtime.pr_creator import PullRequestCreator, PullRequestResult
from awf.runtime.validation import ValidationResult, ValidationRunner
from awf.service.pr_monitor_adoption import PullRequestMonitorAdoptionService
from tests.postgres import create_postgres_test_engine, postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
)

from .executor_paths import _test_worktree_path, _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.mark.unit
async def test_executor_constructor_requires_terminal_runtime_releaser(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="terminal_runtime_releaser"):
        WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=_NoopResumeCompose(),
            validation=ValidationRunner(
                runner=fake,
                artifacts_dir=tmp_path / "artifacts",
            ),
            pr_creator=PullRequestCreator(fake),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
            ),
        )


def _make_executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    pr_monitor_factory: Any = None,
    compose: Any = None,
    validation: Any = None,
    pr_creator: Any = None,
    log_store: LogStore | None = None,
    terminal_releaser: Any = None,
) -> WorkspaceExecutor:
    compose = compose or _NoopResumeCompose()
    validation = validation or ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    pr = pr_creator or PullRequestCreator(fake)
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=compose,
        validation=validation,
        pr_creator=pr,
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
        pr_monitor_factory=pr_monitor_factory,
        log_store=log_store,
        terminal_runtime_releaser=terminal_releaser or _RecordingTerminalRuntimeReleaser(),
    )


class _NoopResumeCompose:
    async def ensure_project_up(
        self,
        *,
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        wait: bool = True,
    ) -> None:
        del project_name, compose_file, workspace_id, wait


class _RecordingValidation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.coverage_calls: list[str | None] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        return ValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        phase = _kwargs.get("phase")
        self.coverage_calls.append(phase if isinstance(phase, str) else None)


class _ExplodingValidation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> SimpleNamespace:
        self.calls.append(phase_names)
        if phase_names == ("post_agent", "validate"):
            raise RuntimeError("docker compose validation failed")
        return SimpleNamespace(all_passed=True, first_failure=None)


class _CancellingSetupValidation:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> SimpleNamespace:
        assert phase_names == ("setup", "pre_agent")
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCELLED")
            await s.commit()
        return SimpleNamespace(all_passed=True, first_failure=None)

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class _CancellingSuccessfulValidation:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        if phase_names == ("post_agent", "validate"):
            async with self._factory() as s:
                repo = WorkspaceRepository(s)
                ws = await repo.get(workspace_id)
                assert ws is not None
                await repo.transition(
                    ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCELLED"
                )
                await s.commit()
        return ValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class _DivergingPrCreator:
    def __init__(self, factory: async_sessionmaker[AsyncSession], workspace_id: str) -> None:
        self._factory = factory
        self._workspace_id = workspace_id

    async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(self._workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="TEST_DIVERGED")
            await s.commit()
        return PullRequestResult(
            url="https://github.com/x/y/pull/42",
            branch=branch_name,
            head_sha="b" * 40,
        )


class _RemovingValidation:
    def __init__(self, worktree_path: Path) -> None:
        self._worktree_path = worktree_path
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        if phase_names == ("post_agent", "validate"):
            shutil.rmtree(self._worktree_path)
        return ValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class _RecordingTerminalRuntimeReleaser:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def release(
        self,
        workspace_id: str,
        *,
        source: str,
        expected_status: WorkspaceStatus | None = None,
    ) -> object:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "source": source,
                "expected_status": expected_status,
            }
        )
        return None


class _RaisingTerminalRuntimeReleaser:
    async def release(
        self,
        workspace_id: str,
        *,
        source: str,
        expected_status: WorkspaceStatus | None = None,
    ) -> object:
        del workspace_id, source, expected_status
        raise RuntimeError("release failed Authorization: Bearer terminal-secret-token")


async def _move_to_operator_control_status(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    final_status: WorkspaceStatus,
) -> None:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_OPERATOR")
        if final_status == WorkspaceStatus.destroyed:
            await repo.transition(ws, to=WorkspaceStatus.destroying, reason_code="TEST_OPERATOR")
            await repo.transition(ws, to=WorkspaceStatus.destroyed, reason_code="TEST_OPERATOR")
        else:
            assert final_status == WorkspaceStatus.cancelled
        await s.commit()


async def _seed_running_with_active_teardown(
    factory: async_sessionmaker[AsyncSession],
    *,
    operation_type: OperationType = OperationType.stop,
) -> tuple[str, str]:
    ws_id = await _seed_ready(factory)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(ws_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        operation = await OperationRepository(s).create(
            workspace_id=ws_id,
            operation_type=operation_type,
            status=OperationStatus.running,
            payload={"source": "operator_api"},
        )
        await s.commit()
        return ws_id, operation.id


@pytest.mark.unit
async def test_terminal_runtime_release_failure_log_redacts_exception_text(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(
        fake,
        factory,
        tmp_path,
        terminal_releaser=_RaisingTerminalRuntimeReleaser(),
    )

    with structlog.testing.capture_logs() as captured:
        await executor._release_terminal_runtime(
            "ws_release_secret",
            expected_status=WorkspaceStatus.failed,
        )

    log_entry = next(
        event
        for event in captured
        if event.get("event") == "executor.terminal_runtime_release_failed"
    )
    assert log_entry["workspace_id"] == "ws_release_secret"
    assert log_entry["expected_status"] == WorkspaceStatus.failed.value
    assert "Authorization: Bearer [redacted]" in log_entry["error"]
    assert "terminal-secret-token" not in log_entry["error"]


@pytest.mark.unit
async def test_transition_if_current_records_blocked_callback_for_active_teardown(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id, operation_id = await _seed_running_with_active_teardown(
        factory,
        operation_type=OperationType.stop,
    )
    executor = _make_executor(fake, factory, tmp_path)

    transitioned = await executor._transition_if_current(
        ws_id,
        from_status=WorkspaceStatus.running,
        to=WorkspaceStatus.validating,
        reason="AGENT_RUN_OK",
        action="start_validation",
    )

    assert transitioned is False
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ignored_events = [
            event for event in ws.events if event.event_type == "workspace.stale_callback_ignored"
        ]
    assert ws.status == WorkspaceStatus.running.value
    assert ignored_events[-1].payload == {
        "callback_source": "executor",
        "callback_action": "start_validation",
        "expected_status": WorkspaceStatus.running.value,
        "actual_status": WorkspaceStatus.running.value,
        "requested_status": WorkspaceStatus.validating.value,
        "operation_id": operation_id,
        "reason_code": "AGENT_RUN_OK",
    }


@pytest.mark.unit
async def test_mark_failed_records_blocked_callback_for_active_teardown(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id, operation_id = await _seed_running_with_active_teardown(
        factory,
        operation_type=OperationType.cancel,
    )
    terminal_releaser = _RecordingTerminalRuntimeReleaser()
    executor = _make_executor(fake, factory, tmp_path, terminal_releaser=terminal_releaser)

    await executor._mark_failed(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        failure_reason=FailureReason.infrastructure_failure,
        message="late executor failure",
        reason_code="EXECUTOR_LATE_FAILURE",
    )

    assert terminal_releaser.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ignored_events = [
            event for event in ws.events if event.event_type == "workspace.stale_callback_ignored"
        ]
    assert ws.status == WorkspaceStatus.running.value
    assert ws.failure_reason is None
    assert ws.failure_message is None
    assert ignored_events[-1].payload == {
        "callback_source": "executor",
        "callback_action": "mark_failed",
        "expected_status": WorkspaceStatus.running.value,
        "actual_status": WorkspaceStatus.running.value,
        "requested_status": WorkspaceStatus.failed.value,
        "operation_id": operation_id,
        "reason_code": "EXECUTOR_LATE_FAILURE",
    }


class _BlockingPrCreator:
    def __init__(self, factory: async_sessionmaker[AsyncSession], workspace_id: str) -> None:
        self._factory = factory
        self._workspace_id = workspace_id

    async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
        async with self._factory() as session:
            await OperationRepository(session).create(
                workspace_id=self._workspace_id,
                operation_type=OperationType.stop,
                status=OperationStatus.running,
                payload={"source": "operator_api"},
            )
            await session.commit()
        return PullRequestResult(
            url="https://github.com/x/y/pull/321",
            branch=branch_name,
            head_sha="c" * 40,
        )


async def _seed_ready(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent: str = "codex",
    base_commit: str | None = "a" * 40,
    auto_merge: bool | None = None,
    resolved_profile: dict[str, Any] | None = None,
    requested_profile: dict[str, Any] | None = None,
    profile_ref: str | None = None,
    task_prompt: str = "p",
    task_policy: dict[str, Any] | None = None,
    owned_paths: list[str] | None = None,
    test_commands: list[str] | None = None,
    task_kind: str = "feature_branch_pr",
    initial_review_grace_period_seconds: float | None = None,
    create_task_attempt: bool = False,
    mark_canonical_attempt: bool = False,
    create_worktree: bool = True,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="err-path",
            task_prompt=task_prompt,
            agent=agent,
            test_commands=test_commands or ["pytest -q"],
            requires_database=False,
            owned_paths=owned_paths,
            task_policy=task_policy,
            profile_ref=profile_ref,
            requested_profile=requested_profile,
            resolved_profile=resolved_profile,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            task_kind=task_kind,
        )
        if create_task_attempt:
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=ws.task_external_id,
                idempotency_key=None,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            attempt = await TaskAttemptRepository(s).create_for_workspace(
                task=task,
                workspace=ws,
            )
            if mark_canonical_attempt:
                attempt.is_canonical_for_merge = True
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = "awf/x"
        ws.remote_push_branch = "awf/x"
        ws.base_commit = base_commit
        ws.compose_project_name = "awf_x"
        if auto_merge is not None:
            ws.auto_merge = auto_merge
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


def _provider_recovery_policy(*, max_same_provider_retries: int) -> dict[str, Any]:
    return {
        "agent_model": "gemini-2.5-pro",
        "pr_monitor": {"review_grace_seconds": 55},
        "provider_recovery": {
            "fallbacks": [
                {
                    "agent": "codex",
                    "provider": "openai",
                    "model": "gpt-5.3-codex",
                }
            ],
            "max_fallback_attempts": 1,
            "max_same_provider_retries": max_same_provider_retries,
            "cooldown_seconds": 30,
            "backoff_seconds": 30,
            "retry_after_cap_seconds": 300,
        },
    }


def _provider_recovery_resolved_profile() -> dict[str, Any]:
    return WorkspaceProfile(
        name="executor-provider-recovery",
        source="test",
        validation={"requested_tier": 2},
        monitor=ProfileMonitor(
            initial_review_grace_period_seconds=55,
            non_check_reviewer_settle_seconds=12,
            non_check_reviewer_logins=["review-bot"],
        ),
    ).model_dump(mode="json")


def _provider_recovery_requested_profile() -> dict[str, Any]:
    return {
        "name": "requested-provider-profile",
        "source": "inline-test",
        "validation": {"requested_tier": 2},
    }


def _parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def _seed_monitoring_pr(
    factory: async_sessionmaker[AsyncSession],
    *,
    branch_name: str | None = "awf/x",
    task_kind: str = "feature_branch_pr",
    pr_number: int | None = 42,
    pr_url: str | None = "https://github.com/x/y/pull/42",
    remote_push_branch: str | None = "awf/x",
    compose_project_name: str | None = "awf_x",
    compose_file_path: str | None = "/tmp/awf/x/compose.yml",
    resolved_profile: dict[str, Any] | None = None,
    auto_merge: bool = True,
    initial_review_grace_period_seconds: float | None = None,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="monitor-resume",
            task_prompt="p",
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            resolved_profile=resolved_profile,
            auto_merge=auto_merge,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
        )
        ws.task_kind = task_kind
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = branch_name
        ws.remote_push_branch = remote_push_branch
        ws.base_commit = "a" * 40
        ws.compose_project_name = compose_project_name
        ws.compose_file_path = compose_file_path
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        ws.pr_url = pr_url
        ws.pr_number = pr_number
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await s.commit()
        return ws.id


class TestConstructorValidation:
    @pytest.mark.unit
    async def test_monitor_and_factory_are_mutually_exclusive(
        self, fake: FakeCommandRunner, tmp_path: Path
    ) -> None:
        """Line 107: supplying both pr_monitor and pr_monitor_factory
        is a programming error — the executor can only use one."""
        engine = await create_postgres_test_engine()
        factory = make_session_factory(engine)

        compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
        pr = PullRequestCreator(fake)
        with pytest.raises(ValueError, match="mutually exclusive"):
            WorkspaceExecutor(
                session_factory=factory,
                runner=fake,
                compose=compose,
                validation=validation,
                pr_creator=pr,
                config=ExecutorConfig(
                    worktrees_root=tmp_path / "w",
                    compose_projects_root=tmp_path / "c",
                    default_models={},
                ),
                pr_monitor=object(),  # type: ignore[arg-type]
                pr_monitor_factory=lambda _adapter: object(),
                terminal_runtime_releaser=_RecordingTerminalRuntimeReleaser(),
            )
        await engine.dispose()


class TestMissingBaseCommit:
    @pytest.mark.unit
    async def test_workspace_without_base_commit_fails_fast(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Lines 192-202: a ``ready`` workspace without ``base_commit``
        is an upstream invariant violation. The executor must refuse to
        run rather than passing the literal string 'None' into a
        ``rev-list`` call."""
        ws_id = await _seed_ready(factory, base_commit=None)
        # Queue the adapter's successful run — we need to exit BEFORE
        # the commit step, not at the adapter call.
        fake.queue_result(returncode=0, stdout="adapter ok")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "base_commit" in (ws.failure_message or "")


class TestUnexpectedErrorDuringAgentRun:
    @pytest.mark.unit
    async def test_provider_no_work_failure_from_stderr_creates_fallback_workspace_and_lineage(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.adapters import base as adapter_base
        from awf.db.enums import AgentRuntime, FailureReason

        class _StderrClassifyingGeminiAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.gemini

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.gemini

            def get_provider(self, model: str | None) -> str:
                return "google"

            def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
                del prompt, model
                return ["gemini", "run"]

        monkeypatch.setitem(
            adapter_base._REGISTRY,
            AgentRuntime.gemini,
            _StderrClassifyingGeminiAdapter,
        )

        resolved_profile = _provider_recovery_resolved_profile()
        requested_profile = _provider_recovery_requested_profile()
        test_commands = [
            "uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py -q"
        ]
        ws_id = await _seed_ready(
            factory,
            agent="gemini",
            task_prompt="Preserve this prompt for fallback execution.",
            task_policy=_provider_recovery_policy(max_same_provider_retries=0),
            owned_paths=["src/awf/control/**", "tests/unit/control/**"],
            profile_ref="python-control",
            requested_profile=requested_profile,
            resolved_profile=resolved_profile,
            test_commands=test_commands,
            auto_merge=False,
            initial_review_grace_period_seconds=55,
            create_task_attempt=True,
            mark_canonical_attempt=True,
        )

        fake.queue_result(
            returncode=1,
            stderr="RESOURCE_EXHAUSTED RetryableQuotaError Retry-After: 90",
        )
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="")
        fake.queue_result(returncode=0, stdout="0\n")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            fallback = (
                await session.execute(select(Workspace).where(Workspace.id != ws_id))
            ).scalar_one()
            attempts = list(
                (
                    await session.execute(
                        select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                    )
                ).scalars()
            )
            operations = list(
                (
                    await session.execute(
                        select(Operation).where(Operation.workspace_id == fallback.id)
                    )
                ).scalars()
            )
            requested_events = list(
                (
                    await session.execute(
                        select(WorkspaceEvent).where(
                            WorkspaceEvent.workspace_id == ws_id,
                            WorkspaceEvent.event_type == "workspace.provider_recovery_requested",
                        )
                    )
                ).scalars()
            )
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.agent_failure.value

            terminal_event = next(e for e in ws.events if e.new_state == "failed")
            payload = terminal_event.payload
            assert isinstance(payload, dict)
            assert payload["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
            details = payload.get("details")
            assert isinstance(details, dict)
            recovery = details.get("provider_recovery")
            assert isinstance(recovery, dict)
            assert recovery["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
            assert recovery["failure_type"] == "quota"
            assert recovery["provider"] == "google"
            assert recovery["model"] == "gemini-2.5-pro"
            assert recovery["retryable"] is True
            assert recovery["retry_after_seconds"] == 90
            assert recovery["cooldown_seconds"] == 90
            assert recovery["fallback_allowed"] is True
            assert recovery["recommended_action"] == (
                "Retry after provider cooldown or dispatch an approved fallback model."
            )
            assert (
                "AGENT_PROVIDER_CAPACITY_EXHAUSTED|quota|google|gemini-2.5-pro"
                in (recovery["failure_fingerprint"])
            )

        assert len(requested_events) == 1
        requested_payload = requested_events[0].payload
        assert isinstance(requested_payload, dict)
        provider_payload = requested_payload["provider_recovery"]
        assert provider_payload["action"] == "fallback"
        assert provider_payload["decision_reason_code"] == "PROVIDER_FALLBACK_SELECTED"
        assert provider_payload["target_agent"] == "codex"
        assert provider_payload["target_provider"] == "openai"
        assert provider_payload["target_model"] == "gpt-5.3-codex"
        assert provider_payload["fallback_attempt_number"] == 1
        assert provider_payload["retry_attempt_number"] == 0

        assert fallback.status == WorkspaceStatus.requested.value
        assert fallback.agent == "codex"
        assert fallback.task_prompt == "Preserve this prompt for fallback execution."
        assert fallback.owned_paths == ["src/awf/control/**", "tests/unit/control/**"]
        assert fallback.test_commands == test_commands
        assert fallback.profile_ref == "python-control"
        assert fallback.requested_profile == requested_profile
        assert fallback.resolved_profile == resolved_profile
        assert fallback.resolved_profile["validation"]["requested_tier"] == 2
        assert fallback.resolved_profile["monitor"]["initial_review_grace_period_seconds"] == 55
        assert fallback.auto_merge is False
        assert fallback.initial_review_grace_period_seconds == 55
        assert fallback.task_kind == "feature_branch_pr"
        assert fallback.task_policy["pr_monitor"] == {"review_grace_seconds": 55}
        state = fallback.task_policy["provider_recovery_state"]
        assert state["source_workspace_id"] == ws_id
        assert state["source_attempt_id"] == attempts[0].id
        assert state["source_task_id"] == attempts[0].task_id
        assert state["source_canonical_attempt_id"] == attempts[0].id
        assert state["source_reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
        assert state["action"] == "fallback"
        assert state["target_provider"] == "openai"
        assert state["target_model"] == "gpt-5.3-codex"
        assert state["fallback_attempt_number"] == 1
        assert state["retry_attempt_number"] == 0

        assert [attempt.workspace_id for attempt in attempts] == [ws_id, fallback.id]
        assert attempts[1].attempt_number == 2
        assert attempts[1].task_id == attempts[0].task_id
        assert attempts[1].parent_attempt_id == attempts[0].id
        assert attempts[1].redispatch_from_attempt_id == attempts[0].id
        assert attempts[1].is_canonical_for_merge is False

        assert requested_payload["new_workspace_id"] == fallback.id
        assert requested_payload["source_attempt_id"] == attempts[0].id
        assert requested_payload["source_task_id"] == attempts[0].task_id
        assert requested_payload["source_canonical_attempt_id"] == attempts[0].id
        assert operations[0].type == "retry"
        assert operations[0].payload["source_workspace_id"] == ws_id
        assert operations[0].payload["source_attempt_id"] == attempts[0].id
        assert operations[0].payload["source_task_id"] == attempts[0].task_id
        assert operations[0].payload["source_canonical_attempt_id"] == attempts[0].id
        assert operations[0].payload["provider_recovery"]["action"] == "fallback"

    @pytest.mark.unit
    async def test_provider_no_work_failure_schedules_same_provider_retry_first(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.adapters import base as adapter_base
        from awf.db.enums import AgentRuntime

        class _StderrClassifyingGeminiAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.gemini

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.gemini

            def get_provider(self, model: str | None) -> str:
                return "google"

            def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
                del prompt, model
                return ["gemini", "run"]

        monkeypatch.setitem(
            adapter_base._REGISTRY,
            AgentRuntime.gemini,
            _StderrClassifyingGeminiAdapter,
        )
        task_policy = _provider_recovery_policy(max_same_provider_retries=1)
        retry_after_seconds = 45
        expected_retry_delay = timedelta(seconds=retry_after_seconds)
        assert task_policy["provider_recovery"]["cooldown_seconds"] < retry_after_seconds
        assert task_policy["provider_recovery"]["backoff_seconds"] < retry_after_seconds

        ws_id = await _seed_ready(
            factory,
            agent="gemini",
            task_policy=task_policy,
            create_task_attempt=True,
        )
        before = datetime.now(UTC)
        fake.queue_result(
            returncode=1,
            stderr=(f"RESOURCE_EXHAUSTED RetryableQuotaError Retry-After: {retry_after_seconds}"),
        )
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="")
        fake.queue_result(returncode=0, stdout="0\n")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        await executor.execute(ws_id)
        after = datetime.now(UTC)

        async with factory() as session:
            retry_workspace = (
                await session.execute(select(Workspace).where(Workspace.id != ws_id))
            ).scalar_one()
            event = (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == ws_id,
                        WorkspaceEvent.event_type == "workspace.provider_recovery_requested",
                    )
                )
            ).scalar_one()

        state = retry_workspace.task_policy["provider_recovery_state"]
        not_before = _parse_utc_datetime(state["not_before"])
        assert retry_workspace.status == WorkspaceStatus.requested.value
        assert retry_workspace.agent == "gemini"
        assert retry_workspace.task_policy["agent_model"] == "gemini-2.5-pro"
        assert state["action"] == "retry"
        assert state["target_provider"] == "google"
        assert state["target_model"] == "gemini-2.5-pro"
        assert state["retry_attempt_number"] == 1
        assert state["fallback_attempt_number"] == 0
        assert before + expected_retry_delay <= not_before <= after + expected_retry_delay
        recovery_payload = event.payload["provider_recovery"]
        assert recovery_payload["action"] == "retry"
        assert recovery_payload["decision_reason_code"] == "PROVIDER_RETRY_DELAYED"
        assert recovery_payload["retry_after_seconds"] == retry_after_seconds
        assert "not_before" in recovery_payload

    @pytest.mark.unit
    async def test_generic_no_work_agent_failure_does_not_create_provider_recovery_attempt(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.adapters import base as adapter_base
        from awf.db.enums import AgentRuntime, FailureReason

        class _GenericFailingCodexAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.codex

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            def get_provider(self, model: str | None) -> str:
                return "openai"

            def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
                del prompt, model
                return ["codex", "exec"]

        monkeypatch.setitem(
            adapter_base._REGISTRY,
            AgentRuntime.codex,
            _GenericFailingCodexAdapter,
        )
        ws_id = await _seed_ready(
            factory,
            agent="codex",
            task_policy=_provider_recovery_policy(max_same_provider_retries=1),
            create_task_attempt=True,
        )
        fake.queue_result(returncode=1, stderr="SyntaxError: invalid syntax")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="")
        fake.queue_result(returncode=0, stdout="0\n")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            workspaces = list((await session.execute(select(Workspace))).scalars())
            operations = list((await session.execute(select(Operation))).scalars())
            provider_events = list(
                (
                    await session.execute(
                        select(WorkspaceEvent).where(
                            WorkspaceEvent.event_type.in_(
                                [
                                    "workspace.provider_recovery_requested",
                                    "workspace.provider_recovery_created",
                                    "workspace.provider_recovery_cooldown",
                                ]
                            )
                        )
                    )
                ).scalars()
            )

        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == FailureReason.agent_failure.value
        assert len(workspaces) == 1
        assert operations == []
        assert provider_events == []

    @pytest.mark.unit
    async def test_generic_exception_in_agent_run_marks_infrastructure_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lines 166-174: any non-AgentRunError exception raised by the
        adapter (e.g. a bug in its own code) must mark the workspace
        failed with ``infrastructure_failure``, not crash the whole
        executor thread."""
        ws_id = await _seed_ready(factory)

        from awf.adapters import base as adapter_base

        class _BoomAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.codex

            def __init__(
                self,
                *,
                runner: Any = None,
                default_model: Any = None,
                default_effort: Any = None,
            ) -> None:
                pass

            def get_provider(self, model: str | None) -> str:
                return "fake"

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            def _cli_args(self, *, prompt: str, model: Any) -> list[str]:
                return []

            async def run(
                self,
                *,
                compose_project: str,
                compose_file: Path,
                prompt: str,
                model: Any = None,
            ) -> Any:
                raise RuntimeError("adapter internal bug")

        monkeypatch.setitem(adapter_base._REGISTRY, AgentRuntime.codex, _BoomAdapter)

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "unexpected error" in (ws.failure_message or "")


class TestOperatorControlRaces:
    @pytest.mark.unit
    @pytest.mark.parametrize("final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed])
    async def test_execute_rechecks_after_claim_before_setup(
        self,
        final_status: WorkspaceStatus,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        validation = _RecordingValidation()
        executor = _make_executor(fake, factory, tmp_path, validation=validation)
        original_claim_ready = executor._claim_ready

        async def _claim_then_operator_control(workspace_id: str, **kwargs: Any) -> Any:
            ws = await original_claim_ready(workspace_id, **kwargs)
            assert ws is not None
            async with factory() as s:
                repo = WorkspaceRepository(s)
                fresh = await repo.get(workspace_id)
                assert fresh is not None
                assert fresh.status == WorkspaceStatus.running.value
            await _move_to_operator_control_status(factory, workspace_id, final_status)
            return ws

        executor._claim_ready = _claim_then_operator_control  # type: ignore[method-assign]

        await executor.execute(ws_id)

        assert validation.calls == []
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == final_status.value
            assert ws.failure_reason is None

    @pytest.mark.unit
    @pytest.mark.parametrize("final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed])
    async def test_resume_pr_monitor_rechecks_after_load_before_compose(
        self,
        final_status: WorkspaceStatus,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        compose_calls: list[str] = []
        monitor_calls: list[str] = []

        class _RecordingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
            ) -> None:
                del project_name, compose_file, wait
                compose_calls.append(workspace_id)

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: _Monitor(),
            compose=_RecordingCompose(),
        )
        original_load_workspace = executor._load_workspace

        async def _load_then_operator_control(workspace_id: str) -> Any:
            ws = await original_load_workspace(workspace_id)
            assert ws is not None
            await _move_to_operator_control_status(factory, workspace_id, final_status)
            return ws

        executor._load_workspace = _load_then_operator_control  # type: ignore[method-assign]

        await executor.resume_pr_monitor(ws_id)

        assert compose_calls == []
        assert monitor_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == final_status.value
            assert ws.failure_reason is None

    @pytest.mark.unit
    async def test_start_push_stops_when_validation_cancelled_workspace(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        validation = _CancellingSuccessfulValidation(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")  # branch drift check
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base
        executor = _make_executor(fake, factory, tmp_path, validation=validation)

        await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
        assert ws.status == WorkspaceStatus.cancelled.value
        assert ws.failure_reason is None
        assert ws.events[-1].event_type == "workspace.stale_action_skipped"
        assert ws.events[-1].payload["action"] == "validate"
        assert any(
            event.event_type == "workspace.stale_callback_ignored"
            and event.payload["callback_action"] == "validate"
            for event in ws.events
        )
        assert not any("push" in call.args for call in fake.calls)


class TestMissingWorktreeFailure:
    @pytest.mark.unit
    async def test_missing_worktree_before_post_agent_commit_marks_infrastructure_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory, create_worktree=False)
        worktree_path = _test_worktree_path(factory, ws_id)
        fake.queue_result(returncode=0, stdout="adapter ok")
        terminal_releaser = _RecordingTerminalRuntimeReleaser()
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            terminal_releaser=terminal_releaser,
        )

        await executor.execute(ws_id)

        git_calls = [call.args for call in fake.calls if call.args[:1] == ["git"]]
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert str(worktree_path) in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "WORKTREE_MISSING"
        assert any(
            event.event_type == "workspace.executor_worktree_missing"
            and event.reason_code == "WORKTREE_MISSING"
            for event in ws.events
        )
        assert git_calls == []
        assert terminal_releaser.calls == [
            {
                "workspace_id": ws_id,
                "source": "executor",
                "expected_status": WorkspaceStatus.failed,
            }
        ]

    @pytest.mark.unit
    async def test_missing_worktree_blocked_by_active_teardown_preserves_failure_evidence(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory, create_worktree=False)
        async with factory() as session:
            await OperationRepository(session).create(
                workspace_id=ws_id,
                operation_type=OperationType.stop,
                status=OperationStatus.running,
                payload={"source": "operator_api"},
            )
            await session.commit()
        worktree_path = _test_worktree_path(factory, ws_id)
        terminal_releaser = _RecordingTerminalRuntimeReleaser()
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            terminal_releaser=terminal_releaser,
        )

        available = await executor._ensure_worktree_available(
            workspace_id=ws_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.ready,
            action="post_agent_commit",
        )

        assert available is False
        assert terminal_releaser.calls == []
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            events = await WorkspaceEventRepository(session).list(workspace_id=ws_id)

        assert ws.status == WorkspaceStatus.ready.value
        assert ws.failure_reason == FailureReason.infrastructure_failure.value
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert str(worktree_path) in (ws.failure_message or "")
        assert any(
            event.event_type == "workspace.executor_worktree_missing"
            and event.reason_code == "WORKTREE_MISSING"
            for event in events
        )
        assert any(
            event.event_type == "workspace.stale_callback_ignored"
            and event.payload["callback_action"] == "post_agent_commit"
            for event in events
        )

    @pytest.mark.unit
    async def test_missing_worktree_before_pr_push_marks_infrastructure_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        worktree_path = _test_worktree_path(factory, ws_id)
        validation = _RemovingValidation(worktree_path)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")  # branch drift check
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base
        executor = _make_executor(fake, factory, tmp_path, validation=validation)

        await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "WORKTREE_MISSING" in (ws.failure_message or "")
        assert str(worktree_path) in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "WORKTREE_MISSING"
        assert not any("push" in call.args for call in fake.calls)
        assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls)

    @pytest.mark.unit
    @pytest.mark.parametrize("final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed])
    async def test_cancelled_or_destroyed_status_wins_over_missing_worktree(
        self,
        final_status: WorkspaceStatus,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory, create_worktree=False)
        fake.queue_result(returncode=0, stdout="adapter ok")
        executor = _make_executor(fake, factory, tmp_path)
        original_recheck_status = executor._recheck_status

        async def _recheck_then_operator_status(
            workspace_id: str,
            *,
            expected: WorkspaceStatus,
            action: str,
            reason_code: str = "EXECUTOR_STALE_STATUS",
        ) -> bool:
            result = await original_recheck_status(
                workspace_id,
                expected=expected,
                action=action,
                reason_code=reason_code,
            )
            if result and action == "post_agent_commit":
                await _move_to_operator_control_status(factory, workspace_id, final_status)
            return result

        executor._recheck_status = _recheck_then_operator_status  # type: ignore[method-assign]

        with structlog.testing.capture_logs() as captured:
            await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None

        assert ws.status == final_status.value
        assert ws.failure_reason is None
        assert any(
            event.get("event") == "executor.skip_stale_status"
            and event.get("action") == "post_agent_commit"
            for event in captured
        )
        assert not any(event.get("event") == "executor.worktree_missing" for event in captured)
        assert not any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "WORKTREE_MISSING"
            for event in ws.events
        )


@pytest.mark.unit
async def test_open_pr_reexecution_guard_releases_terminal_runtime(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(ws_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        ws.pr_url = "https://github.com/x/y/pull/9"
        ws.pr_number = 9
        ws.monitor_started_at = datetime.now(UTC)
        await s.commit()
    terminal_releaser = _RecordingTerminalRuntimeReleaser()
    executor = _make_executor(fake, factory, tmp_path, terminal_releaser=terminal_releaser)

    blocked = await executor._block_open_pr_reexecution_without_recovery(workspace_id=ws_id)

    assert blocked.blocked is True
    assert terminal_releaser.calls == [
        {
            "workspace_id": ws_id,
            "source": "executor",
            "expected_status": WorkspaceStatus.failed,
        }
    ]


class TestAgentWatchdogConfig:
    @pytest.mark.unit
    async def test_executor_passes_agent_watchdog_config_to_adapter_factory(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_id = await _seed_ready(factory)
        captured: dict[str, Any] = {}

        class _Adapter:
            def get_provider(self, model: str | None) -> str:
                return "fake"

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            async def run(
                self,
                *,
                compose_project: str,
                compose_file: Path,
                prompt: str,
                model: str | None = None,
                workspace_id: str | None = None,
            ) -> None:
                del compose_project, compose_file, prompt, model, workspace_id
                raise RuntimeError("stop after adapter factory capture")

        def _get_adapter(_runtime: AgentRuntime, **kwargs: Any) -> _Adapter:
            captured.update(kwargs)
            return _Adapter()

        monkeypatch.setattr(executor_module, "get_adapter", _get_adapter)

        compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
        pr = PullRequestCreator(fake)
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=compose,
            validation=validation,
            pr_creator=pr,
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
                agent_wall_timeout_seconds=12,
                agent_idle_timeout_seconds=3,
            ),
            terminal_runtime_releaser=_RecordingTerminalRuntimeReleaser(),
        )

        await executor.execute(ws_id)

        assert captured["agent_wall_timeout_seconds"] == 12
        assert captured["agent_idle_timeout_seconds"] == 3


class TestBranchDriftRecovery:
    """2026-04-24 incident (T41 Phase 3, ws_9ca6134a): agent CLI
    switched to a custom branch and committed there. pr_creator
    pushed the original empty branch → PR ended up empty.

    Fix: executor detects branch drift before the commit step and
    fast-forwards the expected branch to the agent's HEAD."""

    @pytest.mark.unit
    async def test_drift_with_clean_worktree_is_recovered(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Clean-worktree drift path: agent switched and committed,
        left nothing uncommitted. Recovery: switch back + ff-merge."""
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")  # adapter
        fake.queue_result(returncode=0, stdout="awf/feature-x\n")  # abbrev-ref → drifted
        fake.queue_result(returncode=0, stdout="deadbeef12345\n")  # rev-parse HEAD
        fake.queue_result(returncode=0, stdout="")  # status --porcelain (clean)
        fake.queue_result(returncode=0)  # git switch awf/x
        fake.queue_result(returncode=0)  # git merge --ff-only deadbeef12345
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="a.py\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="sha\n")  # pre-push rev-parse HEAD
        fake.queue_result(returncode=0, stdout="awf/x\n")  # pre-push abbrev-ref
        fake.queue_result(returncode=0, stdout="ab commit\n")  # pre-push log
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/1\n")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
        argvs = [c.args for c in fake.calls]
        # ff-only merge (not reset --hard) — preserves working tree.
        merge_calls = [
            a for a in argvs if "merge" in a and "--ff-only" in a and "deadbeef12345" in a
        ]
        assert len(merge_calls) == 1, f"expected one ``merge --ff-only``; got {argvs}"
        # No ``reset --hard`` against the agent head — reset would wipe WIP.
        reset_calls = [a for a in argvs if "reset" in a and "--hard" in a and "deadbeef12345" in a]
        assert reset_calls == [], (
            f"drift recovery must not ``reset --hard`` the agent's HEAD — "
            f"that would wipe any WIP the agent left. Use ``merge --ff-only``. "
            f"Full argvs: {argvs}"
        )
        switch_calls = [a for a in argvs if "switch" in a and "awf/x" in a]
        assert len(switch_calls) == 1
        # No stash activity when the worktree was clean.
        stash_calls = [a for a in argvs if "stash" in a]
        assert stash_calls == []

    @pytest.mark.unit
    async def test_drift_with_uncommitted_wip_preserves_it(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """CodeRabbit + gemini feedback on PR #7: if the agent drifted
        to ``feature-x``, committed some work, AND left other edits
        uncommitted, the naive ``reset --hard`` would wipe the WIP.
        Recovery must stash WIP → switch → ff-merge → pop."""
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")  # adapter
        fake.queue_result(returncode=0, stdout="awf/feature-x\n")  # abbrev-ref
        fake.queue_result(returncode=0, stdout="deadbeef12345\n")  # rev-parse HEAD
        fake.queue_result(
            returncode=0, stdout=" M src/wip.py\n?? new-untracked.txt\n"
        )  # status: HAS WIP (both modified and untracked)
        fake.queue_result(returncode=0, stdout="Saved working directory")  # stash push
        fake.queue_result(returncode=0)  # git switch awf/x
        fake.queue_result(returncode=0)  # git merge --ff-only deadbeef12345
        fake.queue_result(returncode=0, stdout="On branch awf/x")  # stash pop
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="a.py\n")
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")
        fake.queue_result(returncode=0, stdout="sha\n")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0, stdout="ab commit\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/1\n")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
        argvs = [c.args for c in fake.calls]
        # Stash push BEFORE switch, pop AFTER merge.
        stash_push_calls = [a for a in argvs if "stash" in a and "push" in a]
        stash_pop_calls = [a for a in argvs if "stash" in a and "pop" in a]
        assert len(stash_push_calls) == 1, f"WIP must be stashed before switch; got {argvs}"
        assert len(stash_pop_calls) == 1, f"WIP must be popped after ff-merge; got {argvs}"
        # stash push includes --include-untracked
        assert "--include-untracked" in stash_push_calls[0]

    @pytest.mark.unit
    async def test_drift_stash_pop_conflict_surfaces(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """If ``git stash pop`` conflicts (agent's WIP and the
        fast-forwarded commits touch the same regions), surface it as
        a workspace failure rather than silently leave the operator
        with a dirty tree and no signal."""
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/feature-x\n")
        fake.queue_result(returncode=0, stdout="abc123\n")
        fake.queue_result(returncode=0, stdout=" M conflicted.py\n")
        fake.queue_result(returncode=0, stdout="Saved")  # stash push ok
        fake.queue_result(returncode=0)  # switch ok
        fake.queue_result(returncode=0)  # ff-merge ok
        fake.queue_result(
            returncode=1, stderr="CONFLICT (content): Merge conflict in conflicted.py"
        )  # stash pop FAILS

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "stash pop" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_no_drift_skips_recovery(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")  # current == expected
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="a.py\n")
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="tests ok")
        fake.queue_result(returncode=0, stdout="sha\n")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0, stdout="ab commit\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/1\n")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        argvs = [c.args for c in fake.calls]
        switch_calls = [a for a in argvs if "switch" in a]
        reset_hard_calls = [a for a in argvs if "reset" in a and "--hard" in a]
        assert switch_calls == []
        assert reset_hard_calls == []

    @pytest.mark.unit
    async def test_drift_recovery_switch_fails_marks_workspace_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """If the recovery itself fails (expected branch missing,
        corrupted refs), fail loudly rather than fall back to the
        no-op push that created the original incident."""
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/something-else\n")  # abbrev-ref
        fake.queue_result(returncode=0, stdout="abc123\n")  # rev-parse HEAD
        fake.queue_result(returncode=0, stdout="")  # status (clean)
        fake.queue_result(returncode=1, stderr="fatal: invalid reference: awf/x")  # switch FAILS

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "branch drift" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_branch_drift_check_rev_parse_failure_marks_workspace_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=128, stderr="fatal: bad HEAD")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "branch drift check" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_branch_drift_without_resolvable_agent_head_marks_workspace_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/drifted\n")
        fake.queue_result(returncode=128, stderr="fatal: cannot resolve HEAD")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "agent HEAD could not be resolved" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_branch_drift_status_failure_marks_workspace_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/drifted\n")
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=128, stderr="fatal: status failed")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "git status" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_branch_drift_unstashable_wip_marks_workspace_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/drifted\n")
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=0, stdout=" M src/wip.py\n")
        fake.queue_result(returncode=1, stderr="cannot write index")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "couldn't be stashed" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_branch_drift_switch_failure_with_stash_restores_wip_before_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/drifted\n")
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=0, stdout=" M src/wip.py\n")
        fake.queue_result(returncode=0, stdout="Saved")
        fake.queue_result(returncode=1, stderr="fatal: invalid reference: awf/x")
        fake.queue_result(returncode=0, stdout="restored")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "could not switch back" in (ws.failure_message or "")
        assert any("stash" in call.args and "pop" in call.args for call in fake.calls)

    @pytest.mark.unit
    async def test_branch_drift_merge_failure_with_stash_restores_wip_before_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/drifted\n")
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=0, stdout=" M src/wip.py\n")
        fake.queue_result(returncode=0, stdout="Saved")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=1, stderr="fatal: not possible to fast-forward")
        fake.queue_result(returncode=0, stdout="restored")

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "merge --ff-only" in (ws.failure_message or "")
        assert any("stash" in call.args and "pop" in call.args for call in fake.calls)


class TestCommitStepRuntimeError:
    @pytest.mark.unit
    async def test_nonzero_git_commit_raises_and_marks_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Lines 227 + 318-326: if ``git commit`` exits non-zero, the
        post-agent commit block raises a RuntimeError which is caught
        by the generic except → mark infrastructure_failure."""
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff (non-empty)
        fake.queue_result(
            returncode=1, stderr="nothing to commit, working tree clean"
        )  # git commit FAILS

        executor = _make_executor(fake, factory, tmp_path)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "commit step failed" in (ws.failure_message or "")


class TestValidationInfrastructureError:
    @pytest.mark.unit
    async def test_validation_runner_exception_finishes_validation_run(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        validation = _ExplodingValidation()
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="a.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=128, stderr="fatal: not a git repository")

        executor = _make_executor(fake, factory, tmp_path, validation=validation)
        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "unexpected error during validation run" in (ws.failure_message or "")

            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert len(runs) == 1
            run = runs[0]
            assert run.status == "failed"
            assert run.reason_code == "VALIDATION_INFRASTRUCTURE_ERROR"
            assert run.workspace_head_sha is None
            assert run.finished_at is not None

        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
        assert any(
            call.args[:4] == ["git", "-C", str(_test_worktree_path(factory, ws_id)), "rev-parse"]
            and call.args[-1] == "HEAD"
            for call in fake.calls
        )


class TestPullRequestUnexpectedError:
    @pytest.mark.unit
    def test_salvage_patch_exclusion_supports_linked_worktree_gitdir(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
        relative_worktree = tmp_path / "relative-worktree"
        relative_git_dir = tmp_path / "relative-gitdir"
        relative_worktree.mkdir()
        (relative_git_dir / "info").mkdir(parents=True)
        (relative_worktree / ".git").write_text("gitdir: ../relative-gitdir\n", encoding="utf-8")

        executor._exclude_agent_salvage_artifacts(relative_worktree)
        executor._exclude_agent_salvage_artifacts(relative_worktree)

        relative_exclude = relative_git_dir / "info" / "exclude"
        assert relative_exclude.read_text(encoding="utf-8").splitlines() == ["/.awf/salvage/"]

        absolute_worktree = tmp_path / "absolute-worktree"
        absolute_git_dir = tmp_path / "absolute-gitdir"
        absolute_worktree.mkdir()
        (absolute_git_dir / "info").mkdir(parents=True)
        (absolute_worktree / ".git").write_text(
            f"gitdir: {absolute_git_dir}\n",
            encoding="utf-8",
        )

        executor._exclude_agent_salvage_artifacts(absolute_worktree)

        absolute_exclude = absolute_git_dir / "info" / "exclude"
        assert absolute_exclude.read_text(encoding="utf-8").splitlines() == ["/.awf/salvage/"]

    @pytest.mark.unit
    async def test_execute_stops_when_conformance_salvage_cannot_be_prepared(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        validation = _RecordingValidation()
        ws_id = await _seed_ready(
            factory,
            task_policy={"conformance_salvage": {"source_workspace_id": "ws_source"}},
        )
        executor = _make_executor(fake, factory, tmp_path, validation=validation)

        await executor.execute(ws_id)

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == "infrastructure_failure"
        assert workspace.failure_message is not None
        assert "SALVAGE_PATCH_UNAVAILABLE" in workspace.failure_message
        assert validation.calls == []

    @pytest.mark.unit
    async def test_execute_passes_salvage_conflict_prompt_to_agent(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        patch_path = tmp_path / "conflict-execute.patch"
        patch_path.write_text(
            "diff --git a/src/app.py b/src/app.py\n"
            "index 51f15c8..5f2b6d7 100644\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_prompt="finish original task",
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": digest,
                    "implementation_paths": ["src/app.py"],
                    "remaining_gaps": ["add regression test"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        (worktree_path / "src").mkdir()
        (worktree_path / "src/app.py").write_text("current\n", encoding="utf-8")
        captured: dict[str, str] = {}

        class _CaptureAdapter:
            def get_provider(self, model: str | None) -> str:
                del model
                return "fake"

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            async def run(
                self,
                *,
                compose_project: str,
                compose_file: Path,
                prompt: str,
                model: str | None = None,
                workspace_id: str | None = None,
            ) -> None:
                del compose_project, compose_file, model, workspace_id
                captured["prompt"] = prompt
                raise RuntimeError("stop after prompt capture")

        def _get_adapter(_runtime: AgentRuntime, **_kwargs: Any) -> _CaptureAdapter:
            return _CaptureAdapter()

        monkeypatch.setattr(executor_module, "get_adapter", _get_adapter)
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )

        await executor.execute(ws_id)

        prompt = captured["prompt"]
        assert "Automatic AWF salvage conflict" in prompt
        assert ".awf/salvage/conflict-execute.patch" in prompt
        assert "add regression test" in prompt
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            events = await WorkspaceEventRepository(session).list(workspace_id=ws_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert any(
            event.event_type == "workspace.conformance_salvage_conflict"
            and event.payload["agent_patch_path"] == ".awf/salvage/conflict-execute.patch"
            for event in events
        )

    @pytest.mark.unit
    async def test_clean_conformance_salvage_patch_is_applied_before_agent(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        patch_path = tmp_path / "source.patch"
        patch_path.write_text(
            "diff --git a/src/restored.py b/src/restored.py\n"
            "new file mode 100644\n"
            "index 0000000..7f5af8e\n"
            "--- /dev/null\n"
            "+++ b/src/restored.py\n"
            "@@ -0,0 +1 @@\n"
            "+VALUE = 'restored'\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": digest,
                    "implementation_paths": ["src/restored.py"],
                    "remaining_gaps": ["finish tests"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "applied"
        assert result.prompt_override is None
        assert (worktree_path / "src/restored.py").read_text(encoding="utf-8") == (
            "VALUE = 'restored'\n"
        )
        async with factory() as session:
            events = await WorkspaceEventRepository(session).list(workspace_id=ws_id)
        assert any(
            event.event_type == "workspace.conformance_salvage_applied"
            and event.reason_code == "CONFORMANCE_SALVAGE_APPLIED"
            for event in events
        )

    @pytest.mark.unit
    async def test_conflicting_conformance_salvage_launches_resolver_prompt(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        patch_path = tmp_path / "conflict.patch"
        patch_path.write_text(
            "diff --git a/src/app.py b/src/app.py\n"
            "index 51f15c8..5f2b6d7 100644\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_prompt="finish original task",
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": digest,
                    "implementation_paths": ["src/app.py"],
                    "remaining_gaps": ["add regression test"],
                    "plan_path": "docs/awf-plans/ws_old.md",
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        (worktree_path / "src").mkdir()
        (worktree_path / "src/app.py").write_text("current\n", encoding="utf-8")
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "conflict"
        assert result.prompt_override is not None
        assert "could not be applied cleanly" in result.prompt_override
        assert ".awf/salvage/" in result.prompt_override
        assert "add regression test" in result.prompt_override
        assert (worktree_path / "src/app.py").read_text(encoding="utf-8") == "current\n"
        assert (worktree_path / ".awf/salvage/conflict.patch").exists()
        async with factory() as session:
            events = await WorkspaceEventRepository(session).list(workspace_id=ws_id)
        assert any(
            event.event_type == "workspace.conformance_salvage_conflict"
            and event.reason_code == "CONFORMANCE_SALVAGE_CONFLICT"
            for event in events
        )

    @pytest.mark.unit
    async def test_conformance_salvage_digest_mismatch_marks_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        patch_path = tmp_path / "source.patch"
        patch_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
        ws_id = await _seed_ready(
            factory,
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": "0" * 64,
                    "implementation_paths": ["x"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "failed"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == "infrastructure_failure"
        assert workspace.failure_message is not None
        assert "SALVAGE_PATCH_DIGEST_MISMATCH" in workspace.failure_message

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("salvage", "expected_reason"),
        [
            ({}, "SALVAGE_PATCH_UNAVAILABLE"),
            ({"patch_path": "missing.patch"}, "SALVAGE_PATCH_DIGEST_MISMATCH"),
            (
                {"patch_path": "/tmp/awf-missing-salvage.patch", "patch_sha256": "0" * 64},
                "SALVAGE_PATCH_UNAVAILABLE",
            ),
        ],
    )
    async def test_conformance_salvage_missing_patch_metadata_marks_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        salvage: dict[str, str],
        expected_reason: str,
    ) -> None:
        ws_id = await _seed_ready(
            factory,
            task_policy={"conformance_salvage": {"source_workspace_id": "ws_source", **salvage}},
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        subprocess.run(["git", "init", "-q"], cwd=worktree_path, check=True)
        executor = _make_executor(
            AsyncioSubprocessRunner(),
            factory,
            tmp_path,
            validation=_RecordingValidation(),
        )
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "failed"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.failure_message is not None
        assert expected_reason in workspace.failure_message

    @pytest.mark.unit
    async def test_conformance_salvage_apply_failure_marks_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        patch_path = tmp_path / "source.patch"
        patch_path.write_text("diff --git a/x b/x\n", encoding="utf-8")
        digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ws_id = await _seed_ready(
            factory,
            task_policy={
                "conformance_salvage": {
                    "source_workspace_id": "ws_source",
                    "patch_path": str(patch_path),
                    "patch_sha256": digest,
                    "implementation_paths": ["x"],
                }
            },
        )
        worktree_path = _test_worktree_path(factory, ws_id)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=1, stderr="apply exploded")
        executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                ws_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="TEST",
            )
            assert ws is not None
            await session.commit()

        result = await executor._prepare_conformance_salvage_for_execution(
            workspace_id=ws_id,
            workspace=ws,
            worktree_path=worktree_path,
        )

        assert result is not None
        assert result.status == "failed"
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
        assert workspace is not None
        assert workspace.failure_message is not None
        assert "SALVAGE_PATCH_APPLY_FAILED" in workspace.failure_message

    @pytest.mark.unit
    async def test_plan_only_output_fails_before_validation_and_pr_creation(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _UnexpectedPrCreator:
            async def push_and_open(self, **_kwargs: Any) -> PullRequestResult:
                raise AssertionError("plan-only output must not be pushed")

        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(
            returncode=0,
            stdout=("docs/awf-plans/ws_plan.md\ndocs/awf-plans/ws_plan.conformance.json\n"),
        )
        validation = _RecordingValidation()

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_UnexpectedPrCreator(),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "only AWF plan/conformance artifact" in (ws.failure_message or "")
            assert ws.pr_url is None
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
            assert any(
                event.event_type == "workspace.state_changed"
                and event.reason_code == "PLAN_ONLY_OUTPUT"
                for event in events
            )
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert runs == []

        assert validation.calls == [("setup", "pre_agent")]

    @pytest.mark.unit
    async def test_plan_only_staged_conformance_after_real_commit_is_accepted(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _RecordingPrCreator:
            def __init__(self) -> None:
                self.called = False

            async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
                self.called = True
                return PullRequestResult(
                    url="https://github.com/x/y/pull/123",
                    branch=branch_name,
                    head_sha="b" * 40,
                )

        ws_id = await _seed_ready(factory)
        validation = _RecordingValidation()
        pr_creator = _RecordingPrCreator()

        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(  # only the final conformance artifact remains staged
            returncode=0,
            stdout=f"docs/awf-plans/{ws_id}.conformance.json\n",
        )
        fake.queue_result(  # committed implementation output already exists on the branch
            returncode=0,
            stdout="src/awf/mcp/server.py\ntests/unit/mcp/test_mcp_operator_surfaces.py\n",
        )
        fake.queue_result(returncode=0)  # commit staged conformance artifact
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        fake.queue_result(returncode=0, stdout="validated-head\n")  # pre-validation HEAD

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=pr_creator,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            assert ws.pr_url == "https://github.com/x/y/pull/123"
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
            assert not any(event.reason_code == "PLAN_ONLY_OUTPUT" for event in events)
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert len(runs) == 1
            assert runs[0].status == "succeeded"
            assert runs[0].workspace_head_sha == "validated-head"

        assert pr_creator.called is True
        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]

    @pytest.mark.unit
    async def test_fresh_pr_workspace_defers_final_coverage_to_pr_monitor(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _RecordingPrCreator:
            async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
                return PullRequestResult(
                    url="https://github.com/x/y/pull/123",
                    branch=branch_name,
                    head_sha="b" * 40,
                )

        class _Monitor:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                self.calls.append(workspace_id)

        monitor = _Monitor()
        profile = WorkspaceProfile(
            name="defer-final-coverage",
            source="test",
            phases={"validate": ["pytest tests/unit/cli -q"]},
            validation={
                "strategy": {
                    "baseline_coverage": "skip",
                    "edit_gate": "targeted",
                    "final_gate": "coverage",
                },
                "coverage": {
                    "minimum_percent": 99,
                    "enforce": True,
                    "provider": "python",
                    "command": "pytest --cov=awf --cov-report=term-missing",
                },
            },
        )
        ws_id = await _seed_ready(factory, resolved_profile=profile.model_dump(mode="json"))
        validation = _RecordingValidation()

        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="src/awf/runtime/pr_monitor_runner.py\n")
        fake.queue_result(returncode=0)  # commit staged implementation output
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        fake.queue_result(returncode=0, stdout="validated-head\n")  # pre-validation HEAD

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_RecordingPrCreator(),
            pr_monitor_factory=lambda *_args: monitor,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)

        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
        assert validation.coverage_calls == []
        assert monitor.calls == [ws_id]
        assert len(runs) == 1
        coverage_commands = [cmd for cmd in runs[0].commands if cmd.get("phase") == "coverage"]
        assert coverage_commands
        assert coverage_commands[0]["evidence_status"] == "skipped_by_policy"
        assert coverage_commands[0]["evidence_reason_code"] == "TARGETED_EDIT_GATE"

    @pytest.mark.unit
    async def test_unexpected_pr_creation_error_marks_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _ExplodingPrCreator:
            async def push_and_open(self, **kwargs: object) -> object:
                raise FileNotFoundError("gh")

        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="")  # cached diff empty; agent committed
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        fake.queue_result(returncode=0, stdout="pre-pr-validation-head\n")  # rev-parse HEAD
        fake.queue_result(returncode=0, stdout="tests ok")  # validation

        compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=compose,
            validation=validation,
            pr_creator=_ExplodingPrCreator(),  # type: ignore[arg-type]
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
                default_models={AgentRuntime.codex: "gpt-5"},
            ),
            terminal_runtime_releaser=_RecordingTerminalRuntimeReleaser(),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "unexpected error during PR creation" in (ws.failure_message or "")
            assert "FileNotFoundError" in (ws.failure_message or "")
            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert len(runs) == 1
            assert runs[0].status == "succeeded"
            assert runs[0].target_head_sha is None
            assert runs[0].workspace_head_sha == "pre-pr-validation-head"

    @pytest.mark.unit
    async def test_validation_target_sha_update_failure_keeps_open_pr(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        async def _fail_target_sha_update(
            *,
            validation_run_id: str,
            target_head_sha: str,
        ) -> None:
            raise RuntimeError("metadata database temporarily unavailable")

        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="")
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")
        fake.queue_result(returncode=0, stdout="src/app.py\n")
        fake.queue_result(returncode=0, stdout="deadbeef01\n")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0, stdout="abc1234 commit\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/7\n")

        executor = _make_executor(fake, factory, tmp_path)
        executor._set_validation_run_target_head_sha = _fail_target_sha_update  # type: ignore[method-assign]

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/x/y/pull/7"
            assert ws.pr_number == 7
            assert ws.monitor_last_commit_sha == "deadbeef01"

            runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
            assert len(runs) == 1
            assert runs[0].status == "succeeded"
            assert runs[0].target_head_sha is None


class TestPrMonitorFactoryPath:
    @pytest.mark.unit
    def test_monitor_factory_supports_one_two_and_three_argument_forms(self) -> None:
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy", auto_merge=True)

        def _one_arg(adapter: object) -> tuple[str, object]:
            return ("one", adapter)

        def _two_arg(adapter: object, profile: object) -> tuple[str, object, object]:
            return ("two", adapter, profile)

        def _three_arg(
            adapter: object,
            profile: object,
            workspace: object,
        ) -> tuple[str, object, object, object]:
            return ("three", adapter, profile, workspace)

        assert _call_pr_monitor_factory(
            _one_arg,
            adapter=adapter,
            profile=profile,
            workspace=workspace,
        ) == ("one", adapter)
        assert _call_pr_monitor_factory(
            _two_arg,
            adapter=adapter,
            profile=profile,
            workspace=workspace,
        ) == ("two", adapter, profile)
        assert _call_pr_monitor_factory(
            _three_arg,
            adapter=adapter,
            profile=profile,
            workspace=workspace,
        ) == ("three", adapter, profile, workspace)

    @pytest.mark.unit
    def test_uninspectable_factory_uses_two_argument_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy", auto_merge=True)
        calls: list[tuple[object, object]] = []

        def _monitor_factory(adapter: object, profile: object) -> object:
            calls.append((adapter, profile))
            return "monitor"

        original_signature = executor_module.inspect.signature

        def _signature(callable_: object) -> object:
            if callable_ is _monitor_factory:
                raise ValueError("signature unavailable")
            return original_signature(callable_)

        monkeypatch.setattr(executor_module.inspect, "signature", _signature)

        assert (
            _call_pr_monitor_factory(
                _monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=workspace,
            )
            == "monitor"
        )
        assert calls == [(adapter, profile)]

    @pytest.mark.unit
    def test_adapter_only_factory_preserves_internal_type_error(self) -> None:
        """Adapter-only factory body TypeErrors should not be masked."""
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy")
        factory_error = TypeError("factory body broke")
        factory_calls: list[object] = []

        def _monitor_factory(adapter: object) -> object:
            factory_calls.append(adapter)
            raise factory_error

        with pytest.raises(TypeError, match="factory body broke") as exc_info:
            _call_pr_monitor_factory(
                _monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=workspace,
            )

        assert exc_info.value is factory_error
        assert factory_calls == [adapter]

    @pytest.mark.unit
    def test_three_arg_factory_preserves_internal_type_error(self) -> None:
        adapter = object()
        profile = object()
        workspace = SimpleNamespace(id="ws_policy")
        factory_error = TypeError("factory body broke after accepting workspace")
        factory_calls: list[object] = []

        def _monitor_factory(adapter: object, profile: object, workspace: object) -> object:
            factory_calls.extend([adapter, profile, workspace])
            raise factory_error

        with pytest.raises(TypeError, match="accepting workspace") as exc_info:
            _call_pr_monitor_factory(
                _monitor_factory,
                adapter=adapter,
                profile=profile,
                workspace=workspace,
            )

        assert exc_info.value is factory_error
        assert factory_calls == [adapter, profile, workspace]

    @pytest.mark.unit
    async def test_factory_builds_monitor_once_and_it_runs(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Line 501: when pr_monitor_factory is provided (not a bare
        monitor), the executor calls it with the created adapter and
        drives the resulting monitor's ``run()``."""
        factory_calls: list[Any] = []
        monitor_calls: list[dict[str, Any]] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                monitor_calls.append(
                    {"workspace_id": workspace_id, "compose_project": compose_project}
                )
                # Don't transition — let the executor's existing code finish.

        def _monitor_factory(adapter: Any) -> _FakeMonitor:
            factory_calls.append(adapter)
            return _FakeMonitor()

        ws_id = await _seed_ready(factory)
        # Drive the full happy path through agent→commit→validate→push→create PR.
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0, stdout="awf/x\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        # pr_creator pre-push diagnostics:
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0, stdout="abc commit\n")
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/42\n")  # gh pr create

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)
        await executor.execute(ws_id)

        assert len(factory_calls) == 1  # factory called with adapter exactly once
        assert len(monitor_calls) == 1  # monitor.run fired

    @pytest.mark.unit
    async def test_existing_pr_recovery_pushes_and_resumes_monitor_without_duplicate_create(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        def _monitor_factory(adapter: Any) -> _FakeMonitor:
            del adapter
            return _FakeMonitor()

        ws_id = await _seed_ready(factory)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            workspace.pr_url = "https://github.com/x/y/pull/42"
            workspace.pr_number = 42
            await session.commit()

        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        fake.queue_result(returncode=0, stdout="deadbeef\n")  # rev-parse HEAD
        fake.queue_result(returncode=0, stdout="awf/x\n")  # current branch
        fake.queue_result(returncode=0, stdout="abc commit\n")  # ahead of base
        fake.queue_result(returncode=0)  # git push

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)
        await executor.execute(ws_id)

        assert monitor_calls == [ws_id]
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in fake.calls)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            assert workspace.status == WorkspaceStatus.monitoring_pr.value
            assert workspace.pr_url == "https://github.com/x/y/pull/42"
            assert any(event.reason_code == "PR_UPDATED" for event in workspace.events)

    @pytest.mark.unit
    async def test_executor_passes_workspace_row_to_three_arg_factory(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        factory_workspaces: list[Any] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                return None

        def _monitor_factory(adapter: Any, profile: Any, workspace: Any) -> _FakeMonitor:
            factory_workspaces.append(workspace)
            return _FakeMonitor()

        ws_id = await _seed_ready(factory, auto_merge=False)
        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0, stdout="awf/x\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        fake.queue_result(returncode=0, stdout="deadbeef\n")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0, stdout="abc commit\n")
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/x/y/pull/42\n")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)
        await executor.execute(ws_id)

        assert len(factory_workspaces) == 1
        assert factory_workspaces[0].id == ws_id
        assert factory_workspaces[0].auto_merge is False


class TestPrMonitorResume:
    @pytest.mark.unit
    async def test_resume_pr_monitor_logs_unknown_workspace(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake, factory, tmp_path)

        with structlog.testing.capture_logs() as captured:
            await executor.resume_pr_monitor("ws_never_existed")

        assert fake.calls == []
        assert any(
            event.get("event") == "executor.resume_skip_unknown"
            and event.get("workspace_id") == "ws_never_existed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_resume_pr_monitor_logs_unexpected_status(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)

        def _monitor_factory(*_args: Any) -> object:
            raise AssertionError("monitor factory must not run for non-monitoring workspaces")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        with structlog.testing.capture_logs() as captured:
            await executor.resume_pr_monitor(ws_id)

        assert fake.calls == []
        assert any(
            event.get("event") == "executor.resume_skip_not_monitoring_pr"
            and event.get("workspace_id") == ws_id
            and event.get("status") == WorkspaceStatus.ready.value
            for event in captured
        )

    @pytest.mark.unit
    async def test_resume_pr_monitor_uses_persisted_workspace_metadata(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        compose_file = tmp_path / "persisted-compose" / "compose.yml"
        resolved_profile = WorkspaceProfile(
            name="persisted",
            monitor=ProfileMonitor(initial_review_grace_period_seconds=321),
        ).model_dump(mode="json")
        factory_calls: list[dict[str, Any]] = []
        monitor_calls: list[dict[str, Any]] = []

        class _FakeMonitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                monitor_calls.append(
                    {
                        "workspace_id": workspace_id,
                        "compose_project": compose_project,
                        "compose_file": compose_file,
                    }
                )

        def _monitor_factory(adapter: Any, profile: Any, workspace: Any) -> _FakeMonitor:
            factory_calls.append(
                {
                    "adapter": adapter,
                    "profile_name": profile.name,
                    "profile_grace": profile.monitor.initial_review_grace_period_seconds,
                    "auto_merge": workspace.auto_merge,
                    "workspace_grace": workspace.initial_review_grace_period_seconds,
                    "pr_number": workspace.pr_number,
                    "pr_url": workspace.pr_url,
                    "remote_push_branch": workspace.remote_push_branch,
                }
            )
            return _FakeMonitor()

        ws_id = await _seed_monitoring_pr(
            factory,
            pr_number=77,
            pr_url="https://github.com/x/y/pull/77",
            remote_push_branch="awf/persisted",
            compose_project_name="persisted_project",
            compose_file_path=str(compose_file),
            resolved_profile=resolved_profile,
            auto_merge=False,
            initial_review_grace_period_seconds=12.5,
        )
        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        assert fake.calls == []
        assert len(factory_calls) == 1
        assert factory_calls[0]["profile_name"] == "persisted"
        assert factory_calls[0]["profile_grace"] == 321
        assert factory_calls[0]["auto_merge"] is False
        assert factory_calls[0]["workspace_grace"] == 12.5
        assert factory_calls[0]["pr_number"] == 77
        assert factory_calls[0]["pr_url"] == "https://github.com/x/y/pull/77"
        assert factory_calls[0]["remote_push_branch"] == "awf/persisted"
        assert monitor_calls == [
            {
                "workspace_id": ws_id,
                "compose_project": "persisted_project",
                "compose_file": compose_file,
            }
        ]

    @pytest.mark.unit
    async def test_resume_pr_monitor_restarts_persisted_compose_stack_before_monitor(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        compose_file = tmp_path / "persisted-compose" / "compose.yml"
        compose_file_path = compose_file
        call_order: list[str] = []

        class _RecordingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
            ) -> None:
                call_order.append("compose")
                assert project_name == "persisted_project"
                assert compose_file == compose_file_path
                assert workspace_id == ws_id
                assert wait is True

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                call_order.append("monitor")
                assert call_order == ["compose", "monitor"]
                assert workspace_id == ws_id
                assert compose_project == "persisted_project"
                assert compose_file == compose_file_path

        ws_id = await _seed_monitoring_pr(
            factory,
            compose_project_name="persisted_project",
            compose_file_path=str(compose_file),
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: _Monitor(),
            compose=_RecordingCompose(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert call_order == ["compose", "monitor"]

    @pytest.mark.unit
    async def test_resume_pr_monitor_recovers_feature_branch_remote_push_branch(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(
            factory,
            branch_name="awf/legacy-feature",
            remote_push_branch=None,
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.remote_push_branch == "awf/legacy-feature"
            recovery_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.remote_push_branch_recovered"
            ]
            assert len(recovery_events) == 1
            assert recovery_events[0].reason_code == "REMOTE_PUSH_BRANCH_RECOVERED"
            assert recovery_events[0].payload == {
                "remote_push_branch": "awf/legacy-feature",
                "source": "branch_name",
            }

    @pytest.mark.unit
    async def test_recover_feature_branch_remote_push_branch_skips_ineligible_rows(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake, factory, tmp_path)
        ready_id = await _seed_ready(factory)
        existing_id = await _seed_monitoring_pr(
            factory,
            branch_name="awf/existing",
            remote_push_branch="awf/persisted",
        )
        sync_id = await _seed_monitoring_pr(
            factory,
            task_kind="sync_feature_pr",
            branch_name="feature-sync/local",
            remote_push_branch=None,
        )

        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id="ws_missing",
                remote_push_branch="awf/missing",
            )
            is None
        )
        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id=ready_id,
                remote_push_branch="awf/ready",
            )
            is None
        )
        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id=existing_id,
                remote_push_branch="awf/recovered",
            )
            == "awf/persisted"
        )
        assert (
            await executor._recover_feature_branch_remote_push_branch(
                workspace_id=sync_id,
                remote_push_branch="feature-sync/local",
            )
            is None
        )

    @pytest.mark.unit
    async def test_resume_pr_monitor_does_not_use_stale_recovery_when_status_changed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _UnexpectedCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
            ) -> None:
                del project_name, compose_file, workspace_id, wait
                raise AssertionError("compose must not restart after recovery skips")

        ws_id = await _seed_monitoring_pr(
            factory,
            branch_name="awf/concurrent-feature",
            remote_push_branch=None,
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: object(),
            compose=_UnexpectedCompose(),
        )
        original_load_workspace = executor._load_workspace

        async def _load_then_complete(workspace_id: str) -> Any:
            ws = await original_load_workspace(workspace_id)
            async with factory() as s:
                repo = WorkspaceRepository(s)
                fresh = await repo.get(workspace_id)
                assert fresh is not None
                await repo.transition(
                    fresh,
                    to=WorkspaceStatus.completed,
                    reason_code="CONCURRENT_COMPLETED",
                )
                await s.commit()
            return ws

        executor._load_workspace = _load_then_complete  # type: ignore[method-assign]

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.remote_push_branch is None


class TestExecutorCoverageEdges:
    @pytest.mark.unit
    async def test_setup_phase_failure_marks_service_startup_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(
            factory,
            resolved_profile={
                "name": "setup-fails",
                "phases": {"setup": ["./scripts/setup.sh"]},
            },
        )
        fake.queue_result(returncode=1, stderr="setup exploded")
        executor = _make_executor(fake, factory, tmp_path)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "service_startup_failure"
            assert ws.failure_message == "profile setup failed: ./scripts/setup.sh"
            assert ws.events[-1].reason_code == "SERVICE_STARTUP_FAILURE"

    @pytest.mark.unit
    async def test_sync_feature_pr_skips_agent_validation_and_pr_creation(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []
        validation = _RecordingValidation()

        class _UnexpectedPrCreator:
            async def push_and_open(self, **_kwargs: Any) -> PullRequestResult:
                raise AssertionError("adopted PRs must not create a new PR")

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                assert compose_project == "awf_x"
                assert compose_file == tmp_path / "work" / "compose" / ws_id / "compose.yml"
                monitor_calls.append(workspace_id)

        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            create_task_attempt=True,
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.branch_name = f"feature-sync/{ws_id}"
            ws.remote_push_branch = "feature/existing"
            ws.pr_url = "https://github.com/x/y/pull/42"
            ws.pr_number = 42
            await s.commit()

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_UnexpectedPrCreator(),
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        await executor.execute(ws_id)

        assert monitor_calls == [ws_id]
        assert validation.calls == []
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.monitor_last_commit_sha == "h" * 40
            assert ws.base_commit == "b" * 40
            candidate = (
                await s.execute(select(MergeCandidate).where(MergeCandidate.workspace_id == ws_id))
            ).scalar_one()
            assert candidate.status == "open"
            assert candidate.head_sha == "h" * 40
            assert candidate.base_sha == "b" * 40
            assert candidate.pr_url == "https://github.com/x/y/pull/42"

    @pytest.mark.unit
    async def test_sync_feature_pr_missing_initial_adoption_metadata_fails_cleanly(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        factory_calls: list[str] = []
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy={"pr_adoption": {"head_ref": " "}},
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.remote_push_branch = None
            await s.commit()

        def _monitor_factory(*_args: Any, **_kwargs: Any) -> object:
            factory_calls.append("called")
            raise AssertionError("monitor factory must not run with missing metadata")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=_monitor_factory,
        )

        await executor.execute(ws_id)

        assert factory_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "adopted PR workspace is missing" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "PR_ADOPTION_METADATA_MISSING"
            missing = ws.events[-1].payload["details"]["missing"]
            assert "pr_number" in missing
            assert "pr_url" in missing
            assert "remote_push_branch" in missing
            assert "task_policy.pr_adoption.head_ref" in missing
            assert "task_policy.pr_adoption.base_sha" in missing

    @pytest.mark.unit
    async def test_sync_feature_pr_without_monitor_configuration_fails_cleanly(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )

        executor = _make_executor(fake, factory, tmp_path)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message == (
                "adopted PR monitor handoff failed: no PR monitor configured"
            )
            assert ws.events[-1].reason_code == "PR_ADOPTION_MONITOR_UNAVAILABLE"

    @pytest.mark.unit
    async def test_sync_feature_pr_monitor_factory_exception_fails_cleanly(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "ghp_factorysecret123456"
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )

        def _monitor_factory(*_args: Any, **_kwargs: Any) -> object:
            raise RuntimeError(f"factory exploded Authorization: Bearer {secret}")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=_monitor_factory,
        )

        with structlog.testing.capture_logs() as captured:
            await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "factory exploded" in (ws.failure_message or "")
            assert secret not in (ws.failure_message or "")
            assert "Authorization: Bearer [redacted]" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "PR_ADOPTION_MONITOR_UNAVAILABLE"
        log_entry = next(
            event
            for event in captured
            if event.get("event") == "executor.sync_feature_pr_monitor_build_failed"
        )
        assert "exc_info" not in log_entry
        redacted_traceback = log_entry["redacted_traceback"]
        assert "Traceback" in redacted_traceback
        assert "RuntimeError: factory exploded Authorization: Bearer [redacted]" in (
            redacted_traceback
        )
        assert secret not in redacted_traceback

    @pytest.mark.unit
    def test_redacted_exception_traceback_truncates_large_tracebacks(self) -> None:
        secret = "ghp_tracebacksecret123456"
        try:
            raise RuntimeError(f"factory exploded Authorization: Bearer {secret}\n" + ("x" * 5000))
        except RuntimeError as exc:
            redacted_traceback = executor_module._redacted_exception_traceback(exc)

        assert "Authorization: Bearer [redacted]" in redacted_traceback
        assert secret not in redacted_traceback
        assert redacted_traceback.endswith("...[truncated]")
        assert len(redacted_traceback) <= executor_module._EXCEPTION_TRACEBACK_LIMIT + len(
            "...[truncated]"
        )

    @pytest.mark.unit
    async def test_sync_feature_pr_persisted_metadata_loss_fails_before_monitor_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_runs: list[str] = []
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_runs.append(workspace_id)

        terminal_releaser = _RecordingTerminalRuntimeReleaser()
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
            terminal_releaser=terminal_releaser,
        )

        async def _ensure_available(**_kwargs: Any) -> bool:
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(ws_id)
                assert ws is not None
                metadata = dict(ws.task_policy["pr_adoption"])
                metadata.pop("base_sha")
                ws.task_policy = {"pr_adoption": metadata}
                await s.commit()
            return True

        monkeypatch.setattr(executor, "_ensure_worktree_available", _ensure_available)

        await executor.execute(ws_id)

        assert monitor_runs == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "task_policy.pr_adoption.base_sha" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "PR_ADOPTION_METADATA_MISSING"
        assert terminal_releaser.calls == [
            {
                "workspace_id": ws_id,
                "source": "executor",
                "expected_status": WorkspaceStatus.failed,
            }
        ]

    @pytest.mark.unit
    async def test_sync_feature_pr_persisted_status_change_skips_handoff(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_runs: list[str] = []
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_runs.append(workspace_id)

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        async def _ensure_available(**_kwargs: Any) -> bool:
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(ws_id)
                assert ws is not None
                ws.status = WorkspaceStatus.cancelled.value
                await s.commit()
            return True

        monkeypatch.setattr(executor, "_ensure_worktree_available", _ensure_available)

        await executor.execute(ws_id)

        assert monitor_runs == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
            assert ws.events[-1].payload["action"] == "sync_feature_pr_handoff"

    @pytest.mark.unit
    async def test_sync_feature_pr_recheck_prevents_monitor_run_after_handoff(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_runs: list[str] = []
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            create_task_attempt=True,
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_runs.append(workspace_id)

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args, **_kwargs: _Monitor(),
        )

        async def _recheck_status(
            workspace_id: str,
            *,
            expected: WorkspaceStatus,
            action: str,
            reason_code: str = "EXECUTOR_STALE_STATUS",
        ) -> bool:
            del workspace_id, expected, reason_code
            return action != "run_pr_monitor"

        monkeypatch.setattr(executor, "_recheck_status", _recheck_status)

        await executor.execute(ws_id)

        assert monitor_runs == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.events[-1].reason_code == "PR_MONITOR_ADOPTED"

    @pytest.mark.unit
    async def test_sync_feature_pr_unavailable_worktree_stops_before_monitor_factory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        factory_calls: list[str] = []
        ws_id = await _seed_ready(
            factory,
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "repo_slug": "x/y",
                    "pr_number": 42,
                    "pr_url": "https://github.com/x/y/pull/42",
                    "head_ref": "feature/existing",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
            create_worktree=False,
        )

        def _monitor_factory(*_args: Any, **_kwargs: Any) -> object:
            factory_calls.append("called")
            raise AssertionError("monitor factory must not run without a worktree")

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=_monitor_factory,
        )

        async def _ensure_available(**_kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(executor, "_ensure_worktree_available", _ensure_available)

        await executor.execute(ws_id)

        assert factory_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value

    @pytest.mark.unit
    def test_exclude_agent_salvage_artifacts_handles_gitdir_file(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = _make_executor(fake, factory, tmp_path)
        worktree = tmp_path / "worktree"
        git_dir = tmp_path / "actual.git"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: ../actual.git\n", encoding="utf-8")

        executor._exclude_agent_salvage_artifacts(worktree)

        assert (git_dir / "info" / "exclude").read_text(encoding="utf-8") == ("/.awf/salvage/\n")

    @pytest.mark.unit
    def test_required_adoption_metadata_str_rejects_missing_key(self) -> None:
        with pytest.raises(ValueError, match="missing adoption metadata key: head_sha"):
            _required_metadata_str({}, "head_sha")

    @pytest.mark.unit
    async def test_adopted_sync_feature_pr_handoff_writes_monitor_log_and_redacts_reason(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "ghp_supersecretvalue123"

        async def _fetcher(
            *,
            repo: RepoRef,
            pr_number: int,
        ) -> PullRequestAdoptionMetadata:
            assert repo.slug() == "x/y"
            assert pr_number == 42
            return PullRequestAdoptionMetadata(
                number=42,
                head_ref="feature/existing",
                head_repo_slug="x/y",
                base_ref="development",
                head_sha="h" * 40,
                base_sha="b" * 40,
                state="OPEN",
                is_draft=False,
                closed=False,
                merged=False,
                author="octocat",
                url="https://github.com/x/y/pull/42",
                title="feature: existing",
            )

        async with factory() as s:
            response = await PullRequestMonitorAdoptionService(
                s,
                metadata_fetcher=_fetcher,
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="x/y",
                    pr_number=42,
                    agent="claude_code",
                    reason=f"operator retry with GH_TOKEN={secret}",
                )
            )
            repo = WorkspaceRepository(s)
            ws = await repo.get(response.workspace_id)
            assert ws is not None
            attempt = await TaskAttemptRepository(s).get_by_workspace_id(ws.id)
            assert attempt is not None
            validation_repo = ValidationRunRepository(s)
            run = await validation_repo.start(
                workspace_id=ws.id,
                attempt_id=attempt.id,
                tier=1,
                commands=[],
                base_commit="b" * 40,
                target_branch="feature/existing",
                target_head_sha="h" * 40,
                workspace_head_sha="h" * 40,
                log_stream_refs={},
            )
            await validation_repo.finish(
                run.id,
                status="succeeded",
                reason_code="VALIDATION_OK",
            )
            await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
            ws.branch_name = f"feature-sync/{ws.id}"
            ws.compose_project_name = "awf_x"
            ws.compose_file_path = str(tmp_path / "compose.yml")
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
            await s.commit()

        worktrees_root = tmp_path / "work" / "worktrees"
        (worktrees_root / response.workspace_id).mkdir(parents=True, exist_ok=True)
        fake.queue_result(returncode=0)  # git fetch origin development
        fake.queue_result(returncode=0, stdout="0\n")  # base-behind
        fake.queue_result(returncode=0, stdout=pr_payload(head_sha="h" * 40))
        fake.queue_result(returncode=0)  # gh pr merge
        fake.queue_result(returncode=0, stdout="MERGESHA\n")
        adapter = FakeAdapter()
        sleep_fn = RecordedSleep()
        log_store = LogStore(root=tmp_path / "logs", session_factory=factory)

        def _monitor_factory(*_args: Any, **_kwargs: Any) -> Any:
            return make_runner(
                factory=factory,
                cmd=fake,
                adapter=adapter,
                sleep_fn=sleep_fn,
                worktrees_root=worktrees_root,
                log_store=log_store,
            )

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=_monitor_factory,
            log_store=log_store,
        )

        await executor.execute(response.workspace_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(response.workspace_id)
            assert ws is not None
            streams = await WorkspaceLogStreamRepository(s).list_for_workspace(ws.id)
            operations = list(
                (
                    await s.execute(select(Operation).where(Operation.workspace_id == ws.id))
                ).scalars()
            )
            events = list(
                (
                    await s.execute(
                        select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == ws.id)
                    )
                ).scalars()
            )

        monitor_stream = next(stream for stream in streams if stream.stream_id == "monitor.log")
        monitor_log = Path(monitor_stream.path).read_text()
        durable_payloads = json.dumps(
            {
                "task_policy": ws.task_policy,
                "operations": [op.payload for op in operations],
                "operation_results": [op.result for op in operations],
                "events": [
                    {
                        "event_type": event.event_type,
                        "reason_code": event.reason_code,
                        "payload": event.payload,
                    }
                    for event in events
                ],
                "monitor_log": monitor_log,
            },
            sort_keys=True,
        )

        assert monitor_stream.source == "monitor"
        assert '"event": "monitor.start"' in monitor_log
        assert "PR_MONITOR_ADOPTION_REQUESTED" in durable_payloads
        assert "PR_MONITOR_ADOPTED" in durable_payloads
        assert "MERGE" in durable_payloads
        assert secret not in durable_payloads
        assert "[redacted]" in durable_payloads

    @pytest.mark.unit
    async def test_transition_if_current_records_stale_skip_for_diverged_status(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        executor = _make_executor(fake, factory, tmp_path)

        transitioned = await executor._transition_if_current(
            ws_id,
            from_status=WorkspaceStatus.running,
            to=WorkspaceStatus.validating,
            reason="TEST",
            action="start_validation",
        )

        assert transitioned is False
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
            assert ws.events[-1].payload["action"] == "start_validation"

    @pytest.mark.unit
    async def test_recheck_after_setup_stops_when_workspace_was_cancelled(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=_CancellingSetupValidation(factory),
        )

        await executor.execute(ws_id)

        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
            assert ws.events[-1].payload["action"] == "agent_run"

    @pytest.mark.unit
    async def test_persist_pr_records_stale_skip_when_status_changed_after_push(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout="awf/x\n")  # branch drift check
        fake.queue_result(returncode=0)  # add
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_creator=_DivergingPrCreator(factory, ws_id),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url is None
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
            assert ws.events[-1].payload["action"] == "persist_pr"

    @pytest.mark.unit
    async def test_persist_pr_blocked_by_active_teardown_preserves_pr_metadata(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout="awf/x\n")  # branch drift check
        fake.queue_result(returncode=0)  # add
        fake.queue_result(returncode=0, stdout="a.py\n")  # cached diff
        fake.queue_result(returncode=0)  # commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation
        terminal_releaser = _RecordingTerminalRuntimeReleaser()
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_creator=_BlockingPrCreator(factory, ws_id),
            terminal_releaser=terminal_releaser,
        )

        await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            events = await WorkspaceEventRepository(session).list(workspace_id=ws_id)

        assert ws.status == WorkspaceStatus.pushing.value
        assert ws.pr_url == "https://github.com/x/y/pull/321"
        assert ws.pr_number == 321
        assert terminal_releaser.calls == []
        audit_events = [
            event
            for event in events
            if event.event_type in {"workspace.audit.git_push", "workspace.audit.pr_created"}
        ]
        assert {event.event_type for event in audit_events} == {
            "workspace.audit.git_push",
            "workspace.audit.pr_created",
        }
        assert all(
            event.payload["pr_url"] == "https://github.com/x/y/pull/321" for event in audit_events
        )
        stale_event = next(
            event for event in events if event.event_type == "workspace.stale_callback_ignored"
        )
        assert stale_event.payload["callback_action"] == "persist_pr"

    @pytest.mark.unit
    async def test_resume_pr_monitor_compose_failure_records_warning_and_runs_monitor(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []

        class _FailingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
            ) -> None:
                del project_name, compose_file, workspace_id, wait
                raise ComposeOperationError(
                    operation="up",
                    returncode=1,
                    stdout="",
                    stderr="network unavailable",
                    reason_code="COMPOSE_UP_FAILED",
                )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_FailingCompose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            compose_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.monitor_runtime_restart_failed"
            ]
        assert len(compose_events) == 1
        assert compose_events[0].reason_code == "MONITOR_RECOVERY_COMPOSE_FAILED"
        assert compose_events[0].payload == {
            "compose_project_name": "awf_x",
            "compose_file_path": "/tmp/awf/x/compose.yml",
            "operation": "up",
            "returncode": 1,
            "stderr": "network unavailable",
            "reason_code": "COMPOSE_UP_FAILED",
        }

    @pytest.mark.unit
    async def test_resume_pr_monitor_compose_failure_continues_when_warning_record_fails(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []

        class _OneShotFailingSessionFactory:
            def __init__(self, inner: async_sessionmaker[AsyncSession]) -> None:
                self._inner = inner
                self.fail_next = False

            def __call__(self) -> AsyncSession:
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("session pool exhausted")
                return self._inner()

        session_factory = _OneShotFailingSessionFactory(factory)

        class _FailingCompose:
            async def ensure_project_up(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                wait: bool = True,
            ) -> None:
                del project_name, compose_file, workspace_id, wait
                session_factory.fail_next = True
                raise ComposeOperationError(
                    operation="up",
                    returncode=1,
                    stdout="",
                    stderr="network unavailable",
                    reason_code="COMPOSE_UP_FAILED",
                )

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(
            fake,
            session_factory,
            tmp_path,
            compose=_FailingCompose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        with structlog.testing.capture_logs() as captured:
            await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        assert any(
            entry["event"] == "executor.monitor_runtime_restart_failed_record_failed"
            for entry in captured
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            assert not [
                event
                for event in ws.events
                if event.event_type == "workspace.monitor_runtime_restart_failed"
            ]

    @pytest.mark.unit
    async def test_resume_pr_monitor_never_recreates_pr_or_runs_feature_agent(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        monitor_calls: list[str] = []
        validation = _RecordingValidation()

        class _UnexpectedPrCreator:
            async def push_and_open(self, **_kwargs: Any) -> PullRequestResult:
                raise AssertionError("resume_pr_monitor must not push or create a PR")

        class _Monitor:
            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                assert compose_project == "awf_x"
                assert compose_file == Path("/tmp/awf/x/compose.yml")
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            validation=validation,
            pr_creator=_UnexpectedPrCreator(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        assert validation.calls == []
        assert fake.calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.pr_url == "https://github.com/x/y/pull/42"

    @pytest.mark.unit
    async def test_resume_pr_monitor_factory_failure_marks_recovery_failed(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)

        def _factory(*_args: Any) -> object:
            raise RuntimeError("factory broke")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "failed to build PR monitor" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "MONITOR_RECOVERY_FAILED"

    @pytest.mark.unit
    async def test_resume_pr_monitor_without_configured_monitor_fails_cleanly(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        executor = _make_executor(fake, factory, tmp_path)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message == "monitor recovery: no PR monitor configured"
            assert ws.events[-1].reason_code == "MONITOR_RECOVERY_FAILED"
            assert not [
                event
                for event in ws.events
                if event.event_type == "workspace.remote_push_branch_recovered"
            ]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "field",
        [
            "pr_number",
            "pr_url",
            "compose_project_name",
            "compose_file_path",
        ],
    )
    async def test_missing_monitor_recovery_metadata_fails_cleanly(
        self,
        field: str,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        kwargs: dict[str, Any] = {field: None}
        ws_id = await _seed_monitoring_pr(factory, **kwargs)

        def _monitor_factory(*_args: Any) -> object:
            raise AssertionError("monitor factory must not run for invalid recovery rows")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert field in (ws.failure_message or "")
            assert "monitor recovery" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "MONITOR_RECOVERY_METADATA_MISSING"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "task_kind", ["monitor_release_pr", "sync_release_pr", "sync_feature_pr"]
    )
    async def test_sync_and_release_resume_fail_when_remote_push_branch_is_unknown(
        self,
        task_kind: str,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(
            factory,
            task_kind=task_kind,
            branch_name="release-sync/local-only",
            remote_push_branch=None,
        )

        def _monitor_factory(*_args: Any) -> object:
            raise AssertionError("monitor factory must not run without a safe remote branch")

        executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

        await executor.resume_pr_monitor(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "remote_push_branch" in (ws.failure_message or "")
            assert task_kind in (ws.failure_message or "")
            assert ws.remote_push_branch is None
