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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import execution_flow as executor_execution_flow
from awf.db.enums import (
    AgentRuntime,
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.repositories import (
    OperationRepository,
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

        monkeypatch.setattr(executor_execution_flow, "get_adapter", _get_adapter)

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
        _queue_pre_push_checks(fake, head="sha")
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
        _queue_pre_push_checks(fake, head="sha")
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
        _queue_pre_push_checks(fake, head="sha")
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
        """A non-pre-commit ``git commit`` failure surfaces with the
        structured ``POST_AGENT_COMMIT_FAILED`` reason code (not the
        generic ``INFRASTRUCTURE_FAILURE`` default)."""
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
            assert ws.failure_reason == "infrastructure_failure"
            failed_event = next(
                event
                for event in reversed(ws.events)
                if event.event_type == "workspace.state_changed"
                and event.new_state == WorkspaceStatus.failed.value
            )
            assert failed_event.reason_code == "POST_AGENT_COMMIT_FAILED"


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

    @pytest.mark.unit
    async def test_validation_runner_exception_finishes_recovery_validate_operation(
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

        async with factory() as s:
            op = await OperationRepository(s).create(
                workspace_id=ws_id,
                operation_type=OperationType.validate,
                status=OperationStatus.running,
                payload={"source": "test", "recovery_mode": "validate_only"},
                idempotency_key="test:validation-recovery",
            )
            op_id = op.id
            await s.commit()

        executor = _make_executor(fake, factory, tmp_path, validation=validation)
        await executor.execute(ws_id)

        async with factory() as s:
            op = await OperationRepository(s).get(op_id)
            assert op is not None
            assert op.status == OperationStatus.failed.value
            assert op.error_code == "VALIDATION_INFRASTRUCTURE_ERROR"
            assert op.finished_at is not None
