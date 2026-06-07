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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import helpers as executor_helpers
from awf.control.executor.helpers import (
    _call_pr_monitor_factory,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.profiles.models import ProfileMonitor, WorkspaceProfile
from awf.runtime.logs import LogStore
from awf.runtime.pr_creator import PullRequestCreator, PullRequestResult
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
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
    ) -> None:
        del project_name, compose_file, workspace_id, wait, compose_up_timeout_seconds


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


class TestPullRequestUnexpectedErrorPart002:
    @pytest.mark.unit
    async def test_local_coverage_uses_workspace_cpu_cap_for_parallel_workers(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        profile = WorkspaceProfile(
            name="local-coverage-parallel",
            source="test",
            phases={"validate": ["pytest -q"]},
            validation={
                "strategy": {"baseline_coverage": "skip", "final_gate": "coverage"},
                "coverage": {
                    "minimum_percent": 99,
                    "command": "pytest --cov=awf",
                    "parallel_workers": 20,
                },
            },
        )
        ws_id = await _seed_ready(
            factory,
            resolved_profile=profile.model_dump(mode="json"),
            create_task_attempt=True,
        )
        async with factory() as s:
            attempt = await TaskAttemptRepository(s).get_by_workspace_id(ws_id)
            assert attempt is not None
            await ResourceReservationRepository(s).create(
                workspace_id=ws_id,
                attempt_id=attempt.id,
                node_id="local",
                steady_cpu=3.0,
                steady_memory_gb=10.0,
                peak_cpu=6.0,
                peak_memory_gb=16.0,
                disk_mb=None,
                phase="execution",
            )
            await s.commit()

        validation = _RecordingValidation(coverage_result=_coverage_result(tmp_path))
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
        )

        await executor.execute(ws_id)

        assert validation.coverage_calls == ["coverage"]
        assert validation.coverage_kwargs[0]["parallel_worker_cpu_limit"] == 3

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
        # Final pre-push gates re-derive committed output from git: the plan-only
        # gate diffs base..HEAD (name-only), then the protected-output gate diffs
        # it again (name-status). The branch has real committed work, so both pass.
        fake.queue_result(returncode=0, stdout="src/awf/x.py\n")  # plan-only committed diff
        fake.queue_result(returncode=0, stdout="M\0src/awf/x.py\0")  # protected committed diff

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
    async def test_new_pr_on_bitbucket_fails_fast_before_gh_pr_create(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # BitBucket is a supported forge for monitoring an existing PR, so it clears
        # the executor forge gate — but opening a *new* PR shells `gh pr create`
        # (GitHub-only). A BitBucket feature workspace without an existing PR must
        # fail fast at the push step with PR_CREATE_FORGE_NOT_SUPPORTED instead of
        # mis-routing to `gh`/github.com, so `push_and_open` must never run.
        class _AssertNotCalledPrCreator:
            def __init__(self) -> None:
                self.called = False

            async def push_and_open(self, **_kwargs: object) -> object:
                self.called = True
                raise AssertionError("push_and_open must not run for a new BitBucket PR")

        ws_id = await _seed_ready(factory)
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            ws.repo_url = "git@bitbucket.org:workspace/repo.git"
            await s.commit()

        fake.queue_result(returncode=0, stdout="adapter ok")  # agent
        fake.queue_result(returncode=0, stdout="awf/x\n")  # drift-check: on expected branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="")  # cached diff empty; agent committed
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        fake.queue_result(returncode=0, stdout="pre-pr-validation-head\n")  # rev-parse HEAD
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="src/awf/x.py\n")  # plan-only committed diff
        fake.queue_result(returncode=0, stdout="M\0src/awf/x.py\0")  # protected committed diff

        pr_creator = _AssertNotCalledPrCreator()
        compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=compose,
            validation=validation,
            pr_creator=pr_creator,  # type: ignore[arg-type]
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
                default_models={AgentRuntime.codex: "gpt-5"},
            ),
        )

        await executor.execute(ws_id)

        assert pr_creator.called is False
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "gh pr create" in (ws.failure_message or "")
            assert ws.events[-1].reason_code == "PR_CREATE_FORGE_NOT_SUPPORTED"

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
        _queue_pre_push_checks(fake)
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

        original_signature = executor_helpers.inspect.signature

        def _signature(callable_: object) -> object:
            if callable_ is _monitor_factory:
                raise ValueError("signature unavailable")
            return original_signature(callable_)

        monkeypatch.setattr(executor_helpers.inspect, "signature", _signature)

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
        _queue_pre_push_checks(fake, head="deadbeef")
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
        fake.queue_result(returncode=0, stdout="awf/x\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="a\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        _queue_pre_push_checks(fake, head="deadbeef")
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
        _queue_pre_push_checks(fake, head="deadbeef")
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
                compose_up_timeout_seconds: int = 300,
            ) -> None:
                call_order.append("compose")
                assert project_name == "persisted_project"
                assert compose_file == compose_file_path
                assert workspace_id == ws_id
                assert wait is True
                assert compose_up_timeout_seconds == 300

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
                compose_up_timeout_seconds: int = 300,
            ) -> None:
                del project_name, compose_file, workspace_id, wait, compose_up_timeout_seconds
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
