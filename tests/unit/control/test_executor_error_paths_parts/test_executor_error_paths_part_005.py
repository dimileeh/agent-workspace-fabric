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

import copy
import json
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import constants as executor_constants
from awf.control.executor import helpers as executor_helpers
from awf.control.executor.helpers import (
    _required_metadata_str,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import MergeCandidate
from awf.db.repositories import (
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.profiles.models import ProfileMonitor, WorkspaceProfile
from awf.runtime.logs import LogStore
from awf.runtime.pr_creator import PullRequestCreator, PullRequestResult
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    SETUP_DEPENDENCY_NETWORK_RETRY,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    ValidationRunner,
)
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


def _queue_pre_push_checks(fake: FakeCommandRunner, *, head: str = "deadbeef01") -> None:
    # The final plan-only gate is always evaluated before the protected-output
    # gate, so its committed ``--name-only`` diff is always queued first.
    fake.queue_result(returncode=0, stdout="src/fix.py\n")  # plan-only committed diff
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # committed base..HEAD diff
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-push rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/x\n")  # pre-push abbrev-ref
    fake.queue_result(returncode=0, stdout="ab commit\n")  # pre-push log


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


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
    max_validation_fix_passes: int = 5,
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
            max_validation_fix_passes=max_validation_fix_passes,
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
        pr_monitor_factory=pr_monitor_factory,
        log_store=log_store,
    )


class _NoopResumeCompose:
    async def ensure_project_up(
        self,
        *,
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        wait: bool = True,
        compose_up_timeout_seconds: int = 300,
        force_recreate: bool = False,
        services: tuple[str, ...] = (),
    ) -> None:
        del project_name, compose_file, workspace_id, wait, compose_up_timeout_seconds
        del force_recreate, services


class _RecordingValidation:
    def __init__(
        self,
        *,
        phase_result: ValidationResult | None = None,
        coverage_result: ValidationCoverageResult | None = None,
    ) -> None:
        self._phase_result = phase_result or ValidationResult()
        self._coverage_result = coverage_result
        self.calls: list[tuple[str, ...]] = []
        self.coverage_calls: list[str | None] = []
        self.phase_kwargs: list[dict[str, Any]] = []
        self.coverage_kwargs: list[dict[str, Any]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        self.phase_kwargs.append(dict(kwargs))
        if phase_names == ("setup", "pre_agent"):
            return ValidationResult()
        return self._phase_result

    async def run_profile_coverage(self, **_kwargs: Any) -> ValidationCoverageResult | None:
        self.coverage_kwargs.append(dict(_kwargs))
        phase = _kwargs.get("phase")
        self.coverage_calls.append(phase if isinstance(phase, str) else None)
        return self._coverage_result


class _RecordingPrCreator:
    async def push_and_open(self, *, branch_name: str, **_kwargs: Any) -> PullRequestResult:
        return PullRequestResult(
            url="https://github.com/x/y/pull/123",
            branch=branch_name,
            head_sha="b" * 40,
        )


def _validation_command_result(
    tmp_path: Path,
    *,
    returncode: int,
    reason_code: str,
) -> ValidationCommandResult:
    stdout_path = tmp_path / "validation.stdout"
    stderr_path = tmp_path / "validation.stderr"
    stdout_path.write_text("validation stdout\n", encoding="utf-8")
    stderr_path.write_text("validation stderr\n", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest -q",
        returncode=returncode,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="validate",
        reason_code=reason_code,
    )


def _coverage_result(tmp_path: Path, *, percent: float = 99.5) -> ValidationCoverageResult:
    stdout_path = tmp_path / "coverage.stdout"
    stderr_path = tmp_path / "coverage.stderr"
    stdout_path.write_text(f"TOTAL 10 0 {percent:.1f}%\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    command_result = ValidationCommandResult(
        command="pytest --cov=awf",
        returncode=0,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="coverage",
        reason_code="COVERAGE_OK",
    )
    return ValidationCoverageResult(
        provider="python",
        percent=percent,
        minimum_percent=99.0,
        enforce=True,
        status="passed",
        reason_code="COVERAGE_OK",
        command_result=command_result,
    )


def _setup_dependency_metadata(*, retry_exhausted: bool = False) -> dict[str, object]:
    return {
        "reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE,
        "command": "uv sync --extra dev",
        "package": "docker==7.1.0",
        "host": "files.pythonhosted.org",
        "transient_category": "dns",
        "retryable": True,
        "retry_count": 2 if retry_exhausted else 1,
        "retry_budget": 2,
        "retry_exhausted": retry_exhausted,
        "diagnostic": (
            "Failed to download docker==7.1.0 from files.pythonhosted.org: "
            "failed to lookup address information: No address associated with hostname; "
            "Authorization: Bearer ghp_FAKESECRET0000000"
        ),
    }


def _setup_dependency_command_result(
    tmp_path: Path,
    *,
    returncode: int = 0,
    retry_exhausted: bool = False,
) -> ValidationCommandResult:
    stdout_path = tmp_path / "setup.stdout"
    stderr_path = tmp_path / "setup.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("setup stderr\n", encoding="utf-8")
    return ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=returncode,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code=(SETUP_DEPENDENCY_NETWORK_FAILURE if returncode else "VALIDATION_OK"),
        retry_count=2 if retry_exhausted else 1,
        metadata={
            "setup_dependency_network": _setup_dependency_metadata(retry_exhausted=retry_exhausted)
        },
    )


def _setup_dependency_then_later_failure_command_result(
    tmp_path: Path,
) -> ValidationCommandResult:
    stdout_path = tmp_path / "setup_later_failure.stdout"
    stderr_path = tmp_path / "setup_later_failure.stderr"
    stdout_path.write_text("setup retry stdout\n", encoding="utf-8")
    stderr_path.write_text("local post-install hook failed\n", encoding="utf-8")
    setup_metadata = _setup_dependency_metadata(retry_exhausted=False)
    setup_metadata.update(
        {
            "recovered": False,
            "attempts": [
                {
                    "reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE,
                    "command": "uv sync --extra dev",
                    "package": "docker==7.1.0",
                    "host": "files.pythonhosted.org",
                    "transient_category": "dns",
                    "retryable": True,
                    "attempt": 1,
                    "retry_number": 1,
                }
            ],
            "stream_ids": {},
        }
    )
    return ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code="COMMAND_FAILED",
        retry_count=1,
        metadata={"setup_dependency_network": setup_metadata},
    )


class _SetupDependencyValidation(_RecordingValidation):
    def __init__(self, setup_result: ValidationResult, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._setup_result = setup_result

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        self.phase_kwargs.append(dict(kwargs))
        if phase_names == ("setup", "pre_agent"):
            return self._setup_result
        return self._phase_result


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


def _release_adoption_payload(
    *,
    number: int = 321,
    head_ref: str = "development",
    base_ref: str = "main",
    head_sha: str = "h" * 40,
    base_sha: str = "b" * 40,
) -> str:
    return json.dumps(
        {
            "number": number,
            "headRefName": head_ref,
            "headRepository": {"name": "y", "nameWithOwner": "x/y"},
            "isCrossRepository": False,
            "baseRefName": base_ref,
            "headRefOid": head_sha,
            "baseRefOid": base_sha,
            "state": "OPEN",
            "isDraft": False,
            "author": {"login": "octocat"},
            "url": f"https://github.com/x/y/pull/{number}",
            "title": "Release",
        }
    )


def _release_open_pr_list_payload(*, number: int = 321) -> str:
    return json.dumps(
        [
            {
                "number": number,
                "url": f"https://github.com/x/y/pull/{number}",
                "headRefName": "development",
                "headRefOid": "h" * 40,
                "headRepository": {"name": "y", "nameWithOwner": "x/y"},
                "headRepositoryOwner": {"login": "x"},
            }
        ]
    )


_RELEASE_SYNC_POLICY = {"release_sync": {"source_branch": "development", "target_branch": "main"}}


def _release_sync_policy() -> dict[str, Any]:
    """Return an independent copy so tests can't share nested policy state."""
    return copy.deepcopy(_RELEASE_SYNC_POLICY)


class TestExecutorCoverageEdgesPart001:
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
    async def test_executor_setup_dependency_retry_success_preserves_lineage_and_runs_agent(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(
            factory,
            resolved_profile={
                "name": "setup-retry",
                "phases": {"setup": ["uv sync --extra dev"]},
            },
        )
        validation = _SetupDependencyValidation(
            ValidationResult(
                commands=[
                    _setup_dependency_command_result(
                        tmp_path,
                        returncode=0,
                    )
                ]
            )
        )
        pr_creator = _RecordingPrCreator()

        fake.queue_result(returncode=0, stdout="adapter ok")
        fake.queue_result(returncode=0, stdout="awf/x\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="a.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="validated-head\n")
        # Final pre-push gates re-derive committed output from git: plan-only gate
        # diffs base..HEAD (name-only), then the protected-output gate diffs it
        # again (name-status). The branch has real committed work, so both pass.
        fake.queue_result(returncode=0, stdout="a.py\n")  # plan-only committed diff
        fake.queue_result(returncode=0, stdout="M\0a.py\0")  # protected committed diff

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
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
            retry_events = [
                event
                for event in events
                if event.event_type == "workspace.setup_dependency_network_retry"
            ]
            exhausted_events = [
                event
                for event in events
                if event.event_type == "workspace.setup_dependency_network_retry_exhausted"
            ]
            assert len(retry_events) == 1
            assert exhausted_events == []
            payload = retry_events[0].payload
            assert isinstance(payload, dict)
            assert retry_events[0].reason_code == SETUP_DEPENDENCY_NETWORK_RETRY
            assert payload["reason_code"] == SETUP_DEPENDENCY_NETWORK_RETRY
            assert payload["failure_reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
            assert payload["command"] == "uv sync --extra dev"
            assert payload["package"] == "docker==7.1.0"
            assert payload["host"] == "files.pythonhosted.org"
            assert payload["transient_category"] == "dns"
            assert payload["retryable"] is True
            assert payload["retry_count"] == 1
            assert payload["retry_exhausted"] is False

        assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
        assert len(fake.calls) >= 1

    @pytest.mark.unit
    async def test_executor_setup_dependency_retry_exhausted_marks_precise_setup_failure(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(
            factory,
            resolved_profile={
                "name": "setup-retry",
                "phases": {"setup": ["uv sync --extra dev"]},
            },
        )
        validation = _SetupDependencyValidation(
            ValidationResult(
                commands=[
                    _setup_dependency_command_result(
                        tmp_path,
                        returncode=1,
                        retry_exhausted=True,
                    )
                ]
            )
        )
        executor = _make_executor(fake, factory, tmp_path, validation=validation)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "service_startup_failure"
            assert ws.failure_message == "profile setup failed: uv sync --extra dev"
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
            retry_events = [
                event
                for event in events
                if event.event_type == "workspace.setup_dependency_network_retry"
            ]
            exhausted_events = [
                event
                for event in events
                if event.event_type == "workspace.setup_dependency_network_retry_exhausted"
            ]
            assert len(retry_events) == 1
            assert len(exhausted_events) == 1
            retry_payload = retry_events[0].payload
            assert isinstance(retry_payload, dict)
            assert retry_events[0].reason_code == SETUP_DEPENDENCY_NETWORK_RETRY
            assert retry_payload["reason_code"] == SETUP_DEPENDENCY_NETWORK_RETRY
            assert retry_payload["failure_reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
            assert retry_payload["retry_count"] == 2
            assert retry_payload["retry_exhausted"] is True
            exhausted_payload = exhausted_events[0].payload
            assert isinstance(exhausted_payload, dict)
            assert exhausted_events[0].reason_code == SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED
            assert exhausted_payload["reason_code"] == SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED
            assert exhausted_payload["failure_reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
            assert exhausted_payload["package"] == "docker==7.1.0"
            assert "ghp_FAKESECRET0000000" not in json.dumps(exhausted_payload)
            assert "[redacted]" in json.dumps(exhausted_payload)
            terminal = ws.events[-1]
            assert terminal.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
            assert terminal.payload is not None
            assert terminal.payload["reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
            assert terminal.payload["details"]["package"] == "docker==7.1.0"

        assert validation.calls == [("setup", "pre_agent")]
        assert fake.calls == []

    @pytest.mark.unit
    async def test_executor_setup_dependency_retry_then_later_setup_failure_records_retry_without_terminal_setup_reason(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_ready(
            factory,
            resolved_profile={
                "name": "setup-retry",
                "phases": {"setup": ["uv sync --extra dev"]},
            },
        )
        validation = _SetupDependencyValidation(
            ValidationResult(
                commands=[
                    _setup_dependency_then_later_failure_command_result(tmp_path),
                ]
            )
        )
        executor = _make_executor(fake, factory, tmp_path, validation=validation)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "service_startup_failure"
            assert ws.failure_message == "profile setup failed: uv sync --extra dev"
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
            retry_events = [
                event
                for event in events
                if event.event_type == "workspace.setup_dependency_network_retry"
            ]
            exhausted_events = [
                event
                for event in events
                if event.event_type == "workspace.setup_dependency_network_retry_exhausted"
            ]
            assert len(retry_events) == 1
            assert exhausted_events == []
            retry_payload = retry_events[0].payload
            assert isinstance(retry_payload, dict)
            assert retry_events[0].reason_code == SETUP_DEPENDENCY_NETWORK_RETRY
            assert retry_payload["reason_code"] == SETUP_DEPENDENCY_NETWORK_RETRY
            assert retry_payload["failure_reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
            assert retry_payload["retry_count"] == 1
            assert retry_payload["retry_exhausted"] is False
            terminal = ws.events[-1]
            assert terminal.reason_code == "SERVICE_STARTUP_FAILURE"
            assert terminal.payload is None

        assert validation.calls == [("setup", "pre_agent")]
        assert fake.calls == []

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
        assert validation.calls == [("setup", "pre_agent")]
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
            redacted_traceback = executor_helpers._redacted_exception_traceback(exc)

        assert "Authorization: Bearer [redacted]" in redacted_traceback
        assert secret not in redacted_traceback
        assert redacted_traceback.endswith("...[truncated]")
        assert len(redacted_traceback) <= executor_constants._EXCEPTION_TRACEBACK_LIMIT + len(
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
