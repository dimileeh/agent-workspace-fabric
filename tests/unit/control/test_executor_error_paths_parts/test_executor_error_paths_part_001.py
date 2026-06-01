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
from datetime import UTC, datetime, timedelta
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
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Operation, TaskAttempt, Workspace, WorkspaceEvent
from awf.db.repositories import (
    TaskAttemptRepository,
    TaskRepository,
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
from tests.postgres import create_postgres_test_engine, postgres_test_engine
from tests.unit.control.executor_paths import _test_worktree_path, _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


def _queue_pre_push_checks(
    fake: FakeCommandRunner, *, head: str = "deadbeef01", include_plan_only_diff: bool = False
) -> None:
    if include_plan_only_diff:
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

            def _cli_args(self, *, model: str | None) -> list[str]:
                del model
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

            def _cli_args(self, *, model: str | None) -> list[str]:
                del model
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
    async def test_provider_recovery_explicit_codex_capacity_falls_back_to_configured_default(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.adapters import base as adapter_base

        class _StderrClassifyingCodexAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.codex

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            def get_provider(self, model: str | None) -> str:
                return "openai"

            def _cli_args(self, *, model: str | None) -> list[str]:
                del model
                return ["codex", "exec"]

        monkeypatch.setitem(
            adapter_base._REGISTRY,
            AgentRuntime.codex,
            _StderrClassifyingCodexAdapter,
        )
        override_model = "gpt-5.3-codex-spark"
        configured_default = "gpt-5"
        ws_id = await _seed_ready(
            factory,
            agent="codex",
            task_policy={
                "agent_model": override_model,
                "provider_recovery": {
                    "fallbacks": [],
                    "max_same_provider_retries": 1,
                    "cooldown_seconds": 30,
                    "backoff_seconds": 30,
                    "retry_after_cap_seconds": 300,
                },
            },
            create_task_attempt=True,
        )

        fake.queue_result(
            returncode=1,
            stderr="MODEL_CAPACITY_EXHAUSTED Please try again later.",
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
        assert retry_workspace.status == WorkspaceStatus.requested.value
        assert retry_workspace.agent == "codex"
        assert retry_workspace.task_policy["agent_model"] == configured_default
        assert state["action"] == "fallback"
        assert state["target_provider"] == "openai"
        assert state["target_model"] == configured_default
        assert state["retry_attempt_number"] == 0
        assert state["fallback_attempt_number"] == 1
        assert "not_before" not in state

        recovery_payload = event.payload["provider_recovery"]
        assert recovery_payload["action"] == "fallback"
        assert recovery_payload["decision_reason_code"] == "PROVIDER_FALLBACK_SELECTED"
        assert recovery_payload["target_model"] == configured_default

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

            def _cli_args(self, *, model: str | None) -> list[str]:
                del model
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

            def _cli_args(self, *, model: Any) -> list[str]:
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
                compose_up_timeout_seconds: int = 300,
            ) -> None:
                del project_name, compose_file, wait, compose_up_timeout_seconds
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
        validation_head = "d" * 40
        fake.queue_result(returncode=0, stdout=f"{validation_head}\n")  # validation HEAD
        fake.queue_result(returncode=0, stdout="")  # pre-validation status
        fake.queue_result(returncode=0, stdout="")  # cleanup status
        fake.queue_result(returncode=0, stdout=f"{validation_head}\n")  # restore ref
        fake.queue_result(returncode=0, stdout=f"{validation_head}\n")  # current HEAD
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
        executor = _make_executor(fake, factory, tmp_path)

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
        validation_head = "e" * 40
        fake.queue_result(returncode=0, stdout=f"{validation_head}\n")  # validation HEAD
        fake.queue_result(returncode=0, stdout="")  # pre-validation status
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
