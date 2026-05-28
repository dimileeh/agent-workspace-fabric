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
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import PullRequestAdoptionMetadata, RepoRef
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor import monitor_handoff as executor_monitor_handoff
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Operation, WorkspaceEvent
from awf.db.repositories import (
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node import companion_services
from awf.node.compose_manager import ComposeOperationError
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
from awf.service.pr_monitor_adoption import PullRequestMonitorAdoptionService
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
)

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.mark.unit
def test_companion_env_secret_refresh_read_failure_logs_warning(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.mkdir()
    companion_specs = executor_monitor_handoff.companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:x/backend.git",
                    "environment_secrets": {
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        },
                    },
                }
            ],
        }
    )

    with structlog.testing.capture_logs() as captured:
        executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
            workspace_id="ws_read_failed",
            compose_file=compose_file,
            companion_specs=companion_specs,
            environ={},
        )

    assert any(
        entry["event"] == "executor.resume_companion_env_secret_refresh_read_failed"
        and entry["workspace_id"] == "ws_read_failed"
        and entry["compose_file"] == str(compose_file)
        for entry in captured
    )


@pytest.mark.unit
def test_companion_env_secret_refresh_preserves_required_compose_interpolation(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    environment:
      REQUIRED_TOKEN: "${REQUIRED_TOKEN_SOURCE:?COMPANION_ENV_SECRET_SOURCE_MISSING: companion=backend, target=REQUIRED_TOKEN, provider=env, source=REQUIRED_TOKEN_SOURCE}"
      OPTIONAL_TOKEN: "${OPTIONAL_TOKEN_SOURCE:-}"
""".lstrip(),
        encoding="utf-8",
    )
    companion_specs = executor_monitor_handoff.companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:x/backend.git",
                    "environment_secrets": {
                        "REQUIRED_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "REQUIRED_TOKEN_SOURCE",
                            "required": True,
                        },
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        },
                    },
                }
            ],
        }
    )

    executor_monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
        workspace_id="ws_interpolation",
        compose_file=compose_file,
        companion_specs=companion_specs,
        environ={"REQUIRED_TOKEN_SOURCE": "raw-required-secret"},
    )

    rendered = compose_file.read_text(encoding="utf-8")
    assert "OPTIONAL_TOKEN" not in rendered
    assert "'${REQUIRED_TOKEN_SOURCE:?" not in rendered
    assert 'REQUIRED_TOKEN: "${REQUIRED_TOKEN_SOURCE:?' in rendered
    assert "raw-required-secret" not in rendered


@pytest.mark.unit
def test_present_optional_companion_env_secret_refs_uses_public_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def _fake_optional_env_secret_compose_placeholder(value_from: str) -> str:
        captured.append(value_from)
        return "${CANONICAL:-sentinel}"

    monkeypatch.setattr(
        executor_monitor_handoff,
        "optional_env_secret_compose_placeholder",
        _fake_optional_env_secret_compose_placeholder,
    )
    companion_specs = (
        companion_services.WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:api.git",
            environment_secrets=(
                companion_services.CompanionEnvironmentSecretRef(
                    target="OPTIONAL_TOKEN",
                    value_from="OPTIONAL_TOKEN_SOURCE",
                    required=False,
                ),
            ),
        ),
    )

    assert executor_monitor_handoff._present_optional_companion_env_secret_refs(
        companion_specs=companion_specs,
        environ={"OPTIONAL_TOKEN_SOURCE": "raw-optional-secret"},
    ) == {"backend": {"OPTIONAL_TOKEN": "${CANONICAL:-sentinel}"}}
    assert captured == ["OPTIONAL_TOKEN_SOURCE"]


@pytest.mark.unit
def test_restore_compose_environment_list_refs_counts_duplicate_targets_once() -> None:
    environment: list[object] = [
        "OPTIONAL_TOKEN=stale-one",
        "OPTIONAL_TOKEN=stale-two",
        "APP_ENV=test",
    ]

    restored_count = executor_monitor_handoff._restore_compose_environment_list_refs(
        environment,
        {"OPTIONAL_TOKEN": "${OPTIONAL_TOKEN_SOURCE:-}"},
    )

    assert restored_count == 1
    assert environment == [
        "OPTIONAL_TOKEN=${OPTIONAL_TOKEN_SOURCE:-}",
        "OPTIONAL_TOKEN=${OPTIONAL_TOKEN_SOURCE:-}",
        "APP_ENV=test",
    ]


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
    task_policy: dict[str, Any] | None = None,
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
            task_policy=task_policy,
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


class TestExecutorCoverageEdgesPart002:
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
                compose_up_timeout_seconds: int = 300,
            ) -> None:
                del project_name, compose_file, workspace_id, wait, compose_up_timeout_seconds
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
                compose_up_timeout_seconds: int = 300,
            ) -> None:
                del project_name, compose_file, workspace_id, wait, compose_up_timeout_seconds
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
    async def test_resume_pr_monitor_passes_timeouts_to_adapter(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        captured: dict[str, Any] = {}

        def _get_adapter(_runtime: AgentRuntime, **kwargs: Any) -> object:
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(executor_monitor_handoff, "get_adapter", _get_adapter)

        monitor_calls: list[str] = []

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            pr_monitor_factory=lambda *_args: _Monitor(),
        )
        await executor.resume_pr_monitor(ws_id)

        assert monitor_calls == [ws_id]
        assert captured["agent_wall_timeout_seconds"] == executor._config.agent_wall_timeout_seconds
        assert captured["agent_idle_timeout_seconds"] == executor._config.agent_idle_timeout_seconds

    @pytest.mark.unit
    async def test_resume_pr_monitor_preserves_companion_compose_timeout(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        captured: dict[str, Any] = {}

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
                captured.update(
                    {
                        "project_name": project_name,
                        "compose_file": compose_file,
                        "workspace_id": workspace_id,
                        "wait": wait,
                        "compose_up_timeout_seconds": compose_up_timeout_seconds,
                    }
                )

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                del workspace_id, compose_project, compose_file

        ws_id = await _seed_monitoring_pr(
            factory,
            resolved_profile=WorkspaceProfile(name="monitor-timeout").model_dump(mode="json"),
            task_policy={
                "companions": [
                    {
                        "name": "slow-api",
                        "repo_url": "git@github.com:x/slow-api.git",
                        "compose_up_timeout_seconds": 900,
                    }
                ],
            },
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_RecordingCompose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        assert captured["workspace_id"] == ws_id
        assert captured["compose_up_timeout_seconds"] == 900

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("source_value", "expected_reason_code"),
        (
            (None, companion_services.COMPANION_ENV_SECRET_SOURCE_MISSING),
            ("", companion_services.COMPANION_ENV_SECRET_SOURCE_EMPTY),
        ),
    )
    async def test_resume_pr_monitor_preserves_required_companion_env_secret_reason_code(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        source_value: str | None,
        expected_reason_code: str,
    ) -> None:
        if source_value is None:
            monkeypatch.delenv("REQUIRED_TOKEN_SOURCE", raising=False)
        else:
            monkeypatch.setenv("REQUIRED_TOKEN_SOURCE", source_value)
        compose_calls: list[str] = []
        monitor_calls: list[str] = []

        class _Compose:
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
                raise ComposeOperationError(
                    operation="up",
                    returncode=1,
                    stdout="",
                    stderr="compose interpolation failed",
                    reason_code="COMPOSE_COMMAND_FAILED",
                )

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                del compose_project, compose_file
                monitor_calls.append(workspace_id)

        ws_id = await _seed_monitoring_pr(
            factory,
            task_policy={
                "companions": [
                    {
                        "name": "backend",
                        "repo_url": "git@github.com:x/backend.git",
                        "environment_secrets": {
                            "REQUIRED_TOKEN": {
                                "provider": "env",
                                "kind": "env",
                                "value_from": "REQUIRED_TOKEN_SOURCE",
                                "required": True,
                            },
                        },
                    }
                ],
            },
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_Compose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        with structlog.testing.capture_logs() as captured:
            await executor.resume_pr_monitor(ws_id)

        assert compose_calls == []
        assert monitor_calls == [ws_id]
        assert any(
            entry["event"] == "executor.resume_companion_env_secret_precheck_failed"
            and entry["workspace_id"] == ws_id
            and entry["reason_code"] == expected_reason_code
            for entry in captured
        )
        assert not any(entry["event"] == "executor.resume_compose_up_failed" for entry in captured)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            compose_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.monitor_runtime_restart_failed"
            ]
        assert len(compose_events) == 1
        assert compose_events[0].reason_code == "MONITOR_RECOVERY_PRECHECK_FAILED"
        assert compose_events[0].payload["operation"] == "companion_env_secret_precheck"
        assert compose_events[0].payload["reason_code"] == expected_reason_code
        assert expected_reason_code in compose_events[0].payload["stderr"]
        assert "REQUIRED_TOKEN_SOURCE" in compose_events[0].payload["stderr"]

    @pytest.mark.unit
    async def test_resume_pr_monitor_omits_missing_optional_companion_env_secret(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OPTIONAL_TOKEN_SOURCE", raising=False)
        monkeypatch.setenv("PRESENT_TOKEN_SOURCE", "raw-present-secret")
        compose_file = tmp_path / "persisted-compose" / "compose.yml"
        compose_file.parent.mkdir(parents=True)
        compose_file.write_text(
            """
services:
  backend:
    environment:
      APP_ENV: "test"
      OPTIONAL_TOKEN: "${OPTIONAL_TOKEN_SOURCE:-}"
      PRESENT_TOKEN: "${PRESENT_TOKEN_SOURCE:-}"
  agent:
    image: "awf-agent-runtime:latest"
""".lstrip(),
            encoding="utf-8",
        )

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
                del project_name, workspace_id, wait, compose_up_timeout_seconds
                parsed = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
                assert parsed["services"]["backend"]["environment"] == {
                    "APP_ENV": "test",
                    "PRESENT_TOKEN": "${PRESENT_TOKEN_SOURCE:-}",
                }
                assert "raw-present-secret" not in compose_file.read_text(encoding="utf-8")

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                del workspace_id, compose_project, compose_file

        ws_id = await _seed_monitoring_pr(
            factory,
            compose_file_path=str(compose_file),
            task_policy={
                "companions": [
                    {
                        "name": "backend",
                        "repo_url": "git@github.com:x/backend.git",
                        "environment_secrets": {
                            "OPTIONAL_TOKEN": {
                                "provider": "env",
                                "kind": "env",
                                "value_from": "OPTIONAL_TOKEN_SOURCE",
                                "required": False,
                            },
                            "PRESENT_TOKEN": {
                                "provider": "env",
                                "kind": "env",
                                "value_from": "PRESENT_TOKEN_SOURCE",
                                "required": False,
                            },
                        },
                    }
                ],
            },
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_RecordingCompose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

    @pytest.mark.unit
    async def test_resume_pr_monitor_restores_present_optional_companion_env_secret_placeholder(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPTIONAL_TOKEN_SOURCE", "raw-restored-secret")
        compose_file = tmp_path / "persisted-compose" / "compose.yml"
        compose_file.parent.mkdir(parents=True)
        compose_file.write_text(
            """
services:
  backend:
    environment:
      APP_ENV: "test"
  agent:
    image: "awf-agent-runtime:latest"
""".lstrip(),
            encoding="utf-8",
        )

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
                del project_name, workspace_id, wait, compose_up_timeout_seconds
                parsed = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
                assert parsed["services"]["backend"]["environment"] == {
                    "APP_ENV": "test",
                    "OPTIONAL_TOKEN": "${OPTIONAL_TOKEN_SOURCE:-}",
                }
                assert "raw-restored-secret" not in compose_file.read_text(encoding="utf-8")

        class _Monitor:
            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                del workspace_id, compose_project, compose_file

        ws_id = await _seed_monitoring_pr(
            factory,
            compose_file_path=str(compose_file),
            task_policy={
                "companions": [
                    {
                        "name": "backend",
                        "repo_url": "git@github.com:x/backend.git",
                        "environment_secrets": {
                            "OPTIONAL_TOKEN": {
                                "provider": "env",
                                "kind": "env",
                                "value_from": "OPTIONAL_TOKEN_SOURCE",
                                "required": False,
                            },
                        },
                    }
                ],
            },
        )
        executor = _make_executor(
            fake,
            factory,
            tmp_path,
            compose=_RecordingCompose(),
            pr_monitor_factory=lambda *_args: _Monitor(),
        )

        await executor.resume_pr_monitor(ws_id)

        rendered = compose_file.read_text(encoding="utf-8")
        assert "${OPTIONAL_TOKEN_SOURCE:-}" in rendered
        assert "raw-restored-secret" not in rendered
