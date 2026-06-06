"""Executor recovery branch — validation-only path for monitor-driven dispatch.

Recovery dispatched by the PR monitor (workspaces with a pending
`pr_monitor` validate operation) must NOT re-run planning/agent/feature
execution. The executor must skip Step 1 (`_run_agent_task_with_optional_planning`),
skip Step 1b (post-agent commit + branch-drift recovery), and proceed
directly to validation, push, and monitor handoff. The validation
fix-cycle prompt is allowed because it is `build_fix_prompt`, not the
feature task prompt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populates registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor.recovery_payloads import _get_active_recovery_payload
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace as WorkspaceModel
from awf.db.repositories import (
    OperationRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    SETUP_DEPENDENCY_NETWORK_METADATA_KEY,
    ValidationCommandResult,
    ValidationResult,
    ValidationRunner,
)
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktree_path, _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


def _queue_post_validation_conformance_report_commit(
    fake: FakeCommandRunner, report_path: str
) -> None:
    fake.queue_result(returncode=0)  # git add report
    fake.queue_result(returncode=0, stdout=f"{report_path}\n")  # cached report diff
    fake.queue_result(returncode=0)  # commit refreshed report


async def _force_workspace_status(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    status: WorkspaceStatus,
) -> None:
    async with factory() as s:
        await s.execute(
            sa_update(WorkspaceModel)
            .where(WorkspaceModel.id == workspace_id)
            .values(status=status.value)
        )
        await s.commit()


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


_FEATURE_TASK_PROMPT = "Implement the customer feature flag wiring for the staging dashboard."


def _make_executor(
    *,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    max_fix_passes: int = 5,
    pr_monitor_factory: Any = None,
    validation: Any = None,
) -> WorkspaceExecutor:
    compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    validation = validation or ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    pr = PullRequestCreator(fake)
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
            max_validation_fix_passes=max_fix_passes,
        ),
        pr_monitor_factory=pr_monitor_factory,
    )


class _TerminalAfterSuccessfulValidation:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        final_status: WorkspaceStatus,
    ) -> None:
        self._factory = factory
        self._final_status = final_status
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> Any:
        self.calls.append(phase_names)
        if phase_names == ("post_agent", "validate"):
            await _force_workspace_status(self._factory, workspace_id, self._final_status)
        return SimpleValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class SimpleValidationResult:
    all_passed = True
    first_failure = None
    total_retries = 0
    commands: list[Any] = []
    coverage = None


class _SetupFailureValidation:
    def __init__(self, setup_result: ValidationResult) -> None:
        self._setup_result = setup_result
        self.calls: list[tuple[str, ...]] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        if phase_names == ("setup", "pre_agent"):
            return self._setup_result
        return ValidationResult()

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


class _RecordingSetupFailureValidation(_SetupFailureValidation):
    def __init__(self, setup_result: ValidationResult, events: list[str]) -> None:
        super().__init__(setup_result)
        self._events = events

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **kwargs: Any,
    ) -> ValidationResult:
        if phase_names == ("setup", "pre_agent"):
            self._events.append("setup")
        return await super().run_profile_phases(phase_names=phase_names, **kwargs)


def _setup_dependency_exhausted_result(tmp_path: Path) -> ValidationCommandResult:
    stdout_path = tmp_path / "setup.stdout"
    stderr_path = tmp_path / "setup.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("setup stderr\n", encoding="utf-8")
    return ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code=SETUP_DEPENDENCY_NETWORK_FAILURE,
        retry_count=2,
        metadata={
            SETUP_DEPENDENCY_NETWORK_METADATA_KEY: {
                "reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE,
                "command": "uv sync --extra dev",
                "package": "docker==7.1.0",
                "host": "files.pythonhosted.org",
                "transient_category": "dns",
                "retryable": True,
                "retry_count": 2,
                "retry_budget": 2,
                "retry_exhausted": True,
                "diagnostic": "failed to lookup address information",
            }
        },
    )


def _generic_setup_failure_result(tmp_path: Path) -> ValidationCommandResult:
    stdout_path = tmp_path / "generic-setup.stdout"
    stderr_path = tmp_path / "generic-setup.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("missing local configuration\n", encoding="utf-8")
    return ValidationCommandResult(
        command="./scripts/setup-local.sh",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code="COMMAND_FAILED",
    )


def _setup_dependency_retry_success_result(tmp_path: Path) -> ValidationCommandResult:
    stdout_path = tmp_path / "setup-success.stdout"
    stderr_path = tmp_path / "setup-success.stderr"
    stdout_path.write_text("setup stdout\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=0,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase="setup",
        reason_code="COMMAND_OK",
        retry_count=1,
        metadata={
            SETUP_DEPENDENCY_NETWORK_METADATA_KEY: {
                "reason_code": "COMMAND_OK",
                "command": "uv sync --extra dev",
                "package": "docker==7.1.0",
                "host": "files.pythonhosted.org",
                "transient_category": "dns",
                "retryable": True,
                "retry_count": 1,
                "retry_budget": 2,
                "diagnostic": "temporary lookup failure recovered",
            }
        },
    )


async def _seed_ready_workspace_with_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_url: str = "https://github.com/x/y/pull/1",
    pr_number: int = 1,
    create_worktree: bool = True,
    recovery_mode: str = "validate_only",
    source: str = "pr_monitor",
    operation_type: OperationType = OperationType.validate,
    resolved_profile: dict[str, Any] | None = None,
    recovery_payload_overrides: dict[str, Any] | None = None,
    task_kind: str = "feature_branch_pr",
) -> str:
    """Insert a workspace already in ``ready`` with a pending `pr_monitor`
    validate operation — the shape the monitor's RECOVERY_DISPATCH path
    leaves behind.
    """
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="recovery test",
            task_prompt=_FEATURE_TASK_PROMPT,
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            resolved_profile=resolved_profile,
            task_kind=task_kind,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.monitor_last_commit_sha = "d" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = pr_url
        ws.pr_number = pr_number
        ws.remote_push_branch = ws.branch_name
        # walk through the executor pipeline once, then re-enter ready via
        # RECOVERY_DISPATCH (mirrors the monitor's transition).
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="RECOVERY_DISPATCH")
        payload = {
            "owner": source,
            "source": source,
            "action": recovery_mode,
            "requested_action": "rebase" if recovery_mode == "rebase_only" else "validate",
            "reason": "validation_insufficient_tier",
            "reason_code": "VALIDATION_INSUFFICIENT_TIER",
            "recovery_mode": recovery_mode,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "source_head_sha": ws.monitor_last_commit_sha,
            "source_base_sha": ws.base_commit,
            "target_branch": ws.branch_base,
            "remote_branch": ws.remote_push_branch,
        }
        if recovery_payload_overrides:
            payload.update(recovery_payload_overrides)
        await OperationRepository(s).create(
            workspace_id=ws.id,
            operation_type=operation_type,
            payload=payload,
            idempotency_key=f"{source}:{recovery_mode}:{ws.id}",
        )
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


async def _seed_sync_feature_pr_ready_workspace_with_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_url: str = "https://github.com/x/y/pull/206",
    pr_number: int = 206,
    head_repo_slug: str | None = None,
    source_head_sha: str = "d" * 40,
    source_base_sha: str = "a" * 40,
) -> str:
    """Seed an adopted feature PR workspace after monitor recovery dispatch."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        branch_name = "feature/existing-pr"
        adoption = {
            "repo_slug": "x/y",
            "pr_number": pr_number,
            "pr_url": pr_url,
            "head_ref": branch_name,
            "base_ref": "development",
            "head_sha": source_head_sha,
            "base_sha": source_base_sha,
            "source": "existing_github_pr",
        }
        if head_repo_slug is not None:
            adoption["head_repo_slug"] = head_repo_slug
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="adopted PR recovery test",
            task_prompt="Monitor and validate the existing PR.",
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            task_kind="sync_feature_pr",
            remote_push_branch=branch_name,
            task_policy={
                "pr_adoption": adoption,
            },
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"feature-sync/{ws.id}"
        ws.base_commit = source_base_sha
        ws.monitor_last_commit_sha = source_head_sha
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = pr_url
        ws.pr_number = pr_number
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="PR_ADOPTED")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="RECOVERY_DISPATCH")
        await OperationRepository(s).create(
            workspace_id=ws.id,
            operation_type=OperationType.validate,
            payload={
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "validate_only",
                "requested_action": "validate",
                "reason": "validation_insufficient_tier",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "recovery_mode": "validate_only",
                "pr_number": pr_number,
                "pr_url": pr_url,
                "source_head_sha": source_head_sha,
                "source_base_sha": source_base_sha,
                "target_branch": ws.branch_base,
                "remote_branch": branch_name,
            },
            idempotency_key=f"pr_monitor:validate_only:{ws.id}",
        )
        await s.commit()
        (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


async def _seed_open_pr_ready_workspace_without_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    create_worktree: bool = True,
) -> str:
    """Insert a post-PR workspace that was corrupted back to ``ready`` without
    the monitor/operator recovery operation that makes that step-back safe."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="corrupted post-pr ready row",
            task_prompt=_FEATURE_TASK_PROMPT,
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.monitor_last_commit_sha = "d" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = "https://github.com/x/y/pull/9"
        ws.pr_number = 9
        ws.remote_push_branch = ws.branch_name
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="PR_OPENED")
        assert ws.monitor_started_at is not None
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="LEGACY_READY_RESET")
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


async def _seed_ready_workspace_no_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    create_worktree: bool = True,
) -> str:
    """Seed a workspace in ``ready`` WITHOUT any pr_monitor recovery
    operation (regression guard for the normal feature-execution path)."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="normal feature",
            task_prompt=_FEATURE_TASK_PROMPT,
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


def _queue_push_and_pr(
    fake: FakeCommandRunner, *, pr_url: str = "https://github.com/x/y/pull/1"
) -> None:
    fake.queue_result(returncode=0, stdout="src/fix.py\n")  # final plan-only gate committed diff
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # committed base..HEAD diff
    fake.queue_result(returncode=0, stdout="deadbeef01\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref HEAD
    fake.queue_result(returncode=0, stdout="abc1234 work\n")  # log ahead-of-base
    fake.queue_result(returncode=0)  # git push
    fake.queue_result(returncode=0, stdout=pr_url)  # gh pr create


def _queue_existing_pr_push(fake: FakeCommandRunner, *, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout="src/fix.py\n")  # final plan-only gate committed diff
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # committed base..HEAD diff
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref HEAD
    fake.queue_result(returncode=0, stdout=f"{head[:7]} fix\n")  # log ahead-of-base
    fake.queue_result(returncode=0)  # git push


def _queue_rebase_recovery(fake: FakeCommandRunner) -> None:
    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=0)  # git rebase origin/<base>
    fake.queue_result(returncode=0, stdout="b" * 40 + "\n")  # rev-parse origin/<base>
    fake.queue_result(returncode=0, stdout="c" * 40 + "\n")  # rev-parse HEAD
    fake.queue_result(returncode=0)  # git push --force-with-lease


def _queue_already_synced_rebase_recovery(fake: FakeCommandRunner) -> None:
    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=0)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=0, stdout="b" * 40 + "\n")  # rev-parse origin/<base>
    fake.queue_result(returncode=0, stdout="c" * 40 + "\n")  # rev-parse HEAD


def _all_adapter_args(fake: FakeCommandRunner) -> list[list[str]]:
    """Every `docker compose exec ... codex ...` invocation."""
    return [c.args for c in fake.calls if "exec" in c.args and "codex" in c.args]


def _all_adapter_prompt_values(fake: FakeCommandRunner) -> list[str]:
    """Every prompt streamed to `docker compose exec ... codex ...` on stdin."""
    prompts: list[str] = []
    for call in fake.calls:
        if "exec" not in call.args or "codex" not in call.args:
            continue
        if call.input_bytes is not None:
            prompts.append(call.input_bytes.decode())
    return prompts


def _all_adapter_prompts(fake: FakeCommandRunner) -> str:
    """Concatenate every adapter prompt invocation into a single string for substring search."""
    return "\n".join(_all_adapter_prompt_values(fake))


def _all_push_and_pr_create_calls(fake: FakeCommandRunner) -> list[list[str]]:
    """Every git push or gh pr create invocation."""
    return [
        c.args
        for c in fake.calls
        if ("push" in c.args and "git" in c.args) or (c.args[:3] == ["gh", "pr", "create"])
    ]


@pytest.mark.unit
def test_get_active_recovery_payload_returns_payload_when_pending() -> None:
    """The predicate must surface the recovery payload when an active
    validate-only recovery operation exists, and return ``None`` otherwise."""

    class _FakeOperation:
        def __init__(
            self,
            *,
            status: str,
            payload: object,
            operation_type: str = OperationType.validate.value,
        ) -> None:
            self.status = status
            self.payload = payload
            self.type = operation_type

    class _FakeWorkspace:
        def __init__(self, ops: list[_FakeOperation]) -> None:
            self.operations = ops

    pending = _FakeOperation(
        status=OperationStatus.pending.value,
        payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
    )
    running = _FakeOperation(
        status=OperationStatus.running.value,
        payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
    )
    succeeded = _FakeOperation(
        status=OperationStatus.succeeded.value,
        payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
    )
    operator_api = _FakeOperation(
        status=OperationStatus.pending.value,
        payload={"source": "operator_api", "recovery_mode": "validate_only"},
    )
    operator_rebase = _FakeOperation(
        status=OperationStatus.pending.value,
        payload={"source": "operator_api", "recovery_mode": "rebase_only"},
        operation_type=OperationType.rebase.value,
    )
    operator = _FakeOperation(
        status=OperationStatus.pending.value,
        payload={"source": "operator_api"},
    )
    wrong_type = _FakeOperation(
        status=OperationStatus.pending.value,
        payload={"source": "operator_api", "recovery_mode": "validate_only"},
        operation_type=OperationType.refresh.value,
    )
    invalid_payload = _FakeOperation(
        status=OperationStatus.pending.value,
        payload="operator_api",
    )

    assert _get_active_recovery_payload(_FakeWorkspace([pending])) == pending.payload
    assert _get_active_recovery_payload(_FakeWorkspace([running])) == running.payload
    assert _get_active_recovery_payload(_FakeWorkspace([operator_api])) == operator_api.payload
    assert (
        _get_active_recovery_payload(_FakeWorkspace([operator_rebase])) == operator_rebase.payload
    )
    assert _get_active_recovery_payload(_FakeWorkspace([succeeded])) is None
    assert _get_active_recovery_payload(_FakeWorkspace([operator])) is None
    assert _get_active_recovery_payload(_FakeWorkspace([wrong_type])) is None
    assert _get_active_recovery_payload(_FakeWorkspace([invalid_payload])) is None
    assert _get_active_recovery_payload(_FakeWorkspace([])) is None


@pytest.mark.unit
def test_get_active_recovery_payload_prefers_latest_recovery_operation() -> None:
    """When multiple active recovery operations exist, choose the most recent one."""

    class _FakeOperation:
        def __init__(
            self,
            *,
            status: str,
            payload: object,
            operation_type: str = OperationType.validate.value,
            created_at: datetime | None = None,
            started_at: datetime | None = None,
            op_id: str = "",
        ) -> None:
            self.status = status
            self.payload = payload
            self.type = operation_type
            self.created_at = created_at
            self.started_at = started_at
            self.id = op_id

    class _FakeWorkspace:
        def __init__(self, ops: list[_FakeOperation]) -> None:
            self.operations = ops

    older = _FakeOperation(
        status=OperationStatus.pending.value,
        payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        started_at=datetime(2026, 5, 1, tzinfo=UTC),
        op_id="111",
    )
    newer = _FakeOperation(
        status=OperationStatus.running.value,
        payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
        created_at=datetime(2026, 5, 2, tzinfo=UTC),
        started_at=datetime(2026, 5, 2, tzinfo=UTC),
        op_id="222",
    )

    # Ensure ordering is independent of list materialization order.
    assert _get_active_recovery_payload(_FakeWorkspace([older, newer])) == newer.payload
    assert _get_active_recovery_payload(_FakeWorkspace([newer, older])) == newer.payload


@pytest.mark.unit
async def test_executor_skips_planning_and_agent_run_when_recovery_dispatched(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The recovery branch must NOT call adapter.run with the feature
    task prompt or with any of the planning/execution/conformance
    prompts, and must NOT touch the plan file."""

    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    # Pre-write a sentinel plan file so we can verify recovery does not
    # overwrite it.
    plan_path = _test_worktree_path(factory, ws_id) / "docs" / "awf-plans" / f"{ws_id}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = "# pre-existing plan content — must survive monitor recovery\n"
    plan_path.write_text(sentinel, encoding="utf-8")

    # Recovery skips Step 1/1b. Validation runs once at the same PR head and passes.
    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation

    await executor.execute(ws_id)

    # No adapter prompts at all on a clean validation pass — recovery
    # never enters Step 1, never enters the fix-cycle.
    adapter_invocations = _all_adapter_args(fake)
    assert adapter_invocations == []

    prompts = _all_adapter_prompts(fake)
    # Even if a future revision wires the fix-cycle prompt in, the
    # feature task prompt and planning prompts must NEVER appear.
    assert _FEATURE_TASK_PROMPT not in prompts
    assert "## Planning phase" not in prompts
    assert "## Execution phase" not in prompts
    assert "## Conformance phase" not in prompts

    # The plan file is byte-identical (no rewrite).
    assert plan_path.read_text(encoding="utf-8") == sentinel

    # Workspace handed off to monitor (no monitor wired in this test ⇒ completed).
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        assert any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "RECOVERY_VALIDATION_OK"
            and event.old_state == WorkspaceStatus.validating.value
            and event.new_state == WorkspaceStatus.completed.value
            for event in events
        )
        # The recovery operation is closed cleanly.
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        recovery_ops = [
            op
            for op in ops
            if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
        ]
        assert len(recovery_ops) == 1
        assert recovery_ops[0].status == OperationStatus.succeeded.value


@pytest.mark.unit
async def test_recovery_operation_helpers_start_and_finish_only_recovery_rows(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory)
    async with factory() as s:
        repo = OperationRepository(s)
        non_recovery = await repo.create(
            workspace_id=ws_id,
            operation_type=OperationType.validate,
            payload={"source": "operator_api"},
        )
        running_recovery = await repo.create(
            workspace_id=ws_id,
            operation_type=OperationType.validate,
            payload={"source": "operator_api", "recovery_mode": "validate_only"},
        )
        running_recovery.status = OperationStatus.running.value
        await s.commit()

    await executor._start_pending_recovery_operations(workspace_id=ws_id)

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    by_id = {operation.id: operation for operation in ops}
    assert by_id[non_recovery.id].status == OperationStatus.pending.value
    assert by_id[running_recovery.id].status == OperationStatus.running.value
    started_recovery = next(
        operation for operation in ops if operation.id not in {non_recovery.id, running_recovery.id}
    )
    assert started_recovery.status == OperationStatus.running.value

    await executor._finish_active_recovery_operations(
        workspace_id=ws_id,
        status=OperationStatus.failed,
        reason_code="MONITOR_RECOVERY_SETUP_FAILED",
        error_message="profile setup failed",
    )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    by_id = {operation.id: operation for operation in ops}
    assert by_id[non_recovery.id].status == OperationStatus.pending.value
    for operation_id in {running_recovery.id, started_recovery.id}:
        operation = by_id[operation_id]
        assert operation.status == OperationStatus.failed.value
        assert operation.error_code == "MONITOR_RECOVERY_SETUP_FAILED"
        assert operation.error_message == "profile setup failed"
        assert operation.result == {"reason_code": "MONITOR_RECOVERY_SETUP_FAILED"}


@pytest.mark.unit
async def test_executor_recovery_marks_validate_operation_succeeded_on_clean_pass(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The validate Operation row created by the monitor's
    RECOVERY_DISPATCH must transition pending → succeeded when
    validation passes. ``started_at`` must be earlier than
    ``finished_at`` so observability tooling sees a real lifecycle
    rather than a row that jumped straight from pending to a terminal
    status."""

    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation

    await executor.execute(ws_id)

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    pr_monitor_ops = [
        op
        for op in ops
        if op.type == OperationType.validate.value
        and isinstance(op.payload, dict)
        and op.payload.get("source") == "pr_monitor"
    ]
    assert len(pr_monitor_ops) == 1
    op = pr_monitor_ops[0]
    assert op.status == OperationStatus.succeeded.value
    assert isinstance(op.result, dict)
    assert "validation_run_id" in op.result
    assert op.result["log_stream_refs"] == {
        "commands": [
            {
                "stdout": "validation.01_validate.stdout",
                "stderr": "validation.01_validate.stderr",
            }
        ]
    }
    assert op.started_at is not None
    assert op.finished_at is not None
    assert op.started_at < op.finished_at


@pytest.mark.unit
async def test_operator_api_validate_only_recovery_skips_full_agent_path(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        source="operator_api",
    )

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    operator_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "operator_api"
    ]
    assert len(operator_ops) == 1
    assert operator_ops[0].status == OperationStatus.succeeded.value
    assert isinstance(operator_ops[0].result, dict)
    assert "validation_run_id" in operator_ops[0].result


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_kind", "expected_reason_code", "message_fragment"),
    [
        ("monitor_release_pr", "DEPRECATED_TASK_KIND", "deprecated"),
        ("totally_made_up", "UNSUPPORTED_TASK_KIND", "unsupported task kind"),
    ],
)
async def test_unsupported_task_kind_with_active_recovery_still_fails_fast(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    task_kind: str,
    expected_reason_code: str,
    message_fragment: str,
) -> None:
    """Deprecated/unknown task kinds must fail fast even with an active
    recovery.

    The recovery branch in ``execute`` skips ``_dispatch_non_feature_task_kind``;
    without the unconditional ``_reject_unsupported_task_kind`` guard a
    ``monitor_release_pr`` or unknown kind that re-entered with a pending
    validate-only recovery would resume the validation path instead of being
    rejected — the "silently run as feature work" scenario PR #278 forbids.
    """
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        source="worker_restart",
        task_kind=task_kind,
    )

    # Queue results that WOULD drive the validation path so the test fails
    # loudly (a ValidationRun would be created) if the guard is bypassed.
    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert ws.failure_reason == "policy_failure"
    assert message_fragment in (ws.failure_message or "")
    # The recovery validation path never ran.
    assert runs == []
    assert any(
        event.event_type == "workspace.state_changed"
        and event.reason_code == expected_reason_code
        and event.old_state == WorkspaceStatus.running.value
        and event.new_state == WorkspaceStatus.failed.value
        for event in events
    )
    # The seeded recovery operation was never consumed/succeeded.
    recovery_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "worker_restart"
    ]
    assert len(recovery_ops) == 1
    assert recovery_ops[0].status != OperationStatus.succeeded.value


@pytest.mark.unit
async def test_executor_recovery_closes_operation_row_for_rebase_only_mode(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """``recovery_mode='rebase_only'`` performs a real rebase/push, then
    still closes the monitor-created validate operation cleanly."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    _queue_rebase_recovery(fake)
    fake.queue_result(returncode=0, stdout="c" * 40 + "\n")  # pre-validation rev-parse HEAD
    fake.queue_result(returncode=0, stdout="tests ok")  # validation

    await executor.execute(ws_id)

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
    pr_monitor_ops = [
        op
        for op in ops
        if op.type == OperationType.validate.value
        and isinstance(op.payload, dict)
        and op.payload.get("source") == "pr_monitor"
    ]
    assert len(pr_monitor_ops) == 1
    op = pr_monitor_ops[0]
    assert op.type == OperationType.validate.value
    assert op.payload is not None
    assert op.payload.get("recovery_mode") == "rebase_only"
    assert op.payload.get("requested_tier") == 2
    assert op.status == OperationStatus.succeeded.value
    assert isinstance(op.result, dict)
    assert "validation_run_id" in op.result
    assert op.started_at is not None
    assert op.finished_at is not None
    assert op.started_at < op.finished_at
    assert len(runs) == 1
    assert runs[0].tier >= 2
    assert runs[0].target_head_sha == "c" * 40
    assert runs[0].workspace_head_sha == "c" * 40
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert len(rebase_ops) == 1
    rebase_op = rebase_ops[0]
    assert rebase_op.status == OperationStatus.succeeded.value
    assert rebase_op.idempotency_key is not None
    assert rebase_op.idempotency_key.startswith("pr_monitor:rebase_only:")
    assert rebase_op.started_at is not None
    assert rebase_op.finished_at is not None
    assert rebase_op.payload == {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": "rebase_only",
        "requested_action": "rebase",
        "reason": "validation_insufficient_tier",
        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        "recovery_mode": "rebase_only",
        "pr_number": 1,
        "pr_url": "https://github.com/x/y/pull/1",
        "source_head_sha": "d" * 40,
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{ws_id}",
    }
    assert rebase_op.result == {
        "status": "succeeded",
        "reason_code": "REBASE_OK",
        "source_base_sha": "a" * 40,
        "source_head_sha": "d" * 40,
        "target_base_sha": "b" * 40,
        "target_head_sha": "c" * 40,
        "pushed": True,
        "rebased": True,
    }


@pytest.mark.unit
async def test_executor_recovery_marks_validate_operation_failed_when_validation_fails(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When the recovery validation pass exhausts the fix budget,
    the validate operation row must end in `failed` so observability
    tooling reflects reality."""

    # max_fix_passes=0 → exactly one validation attempt; if it fails,
    # the workspace transitions to ``failed``.
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=0)
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    _queue_validation_head(fake)
    fake.queue_result(
        returncode=1,
        stdout="FAILED tests/foo.py::test_bar",
        stderr="AssertionError",
    )

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(pr_monitor_ops) == 1
    assert pr_monitor_ops[0].status == OperationStatus.failed.value


@pytest.mark.unit
async def test_failed_recovery_operation_includes_reason_code(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A failed recovery operation row must carry the validation failure
    reason_code in its result so observability tooling can classify the
    failure without parsing the error_message."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path, max_fix_passes=0)
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    _queue_validation_head(fake)
    fake.queue_result(
        returncode=1,
        stdout="FAILED tests/foo.py::test_bar",
        stderr="AssertionError",
    )

    await executor.execute(ws_id)

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(pr_monitor_ops) == 1
    assert pr_monitor_ops[0].status == OperationStatus.failed.value
    assert isinstance(pr_monitor_ops[0].result, dict)
    # Phase-level command failures surface the concrete reason code.
    assert pr_monitor_ops[0].result.get("reason_code") == "COMMAND_FAILED"
    assert pr_monitor_ops[0].result.get("log_stream_refs") == {
        "commands": [
            {
                "stdout": "validation.01_validate.stdout",
                "stderr": "validation.01_validate.stderr",
            }
        ]
    }


@pytest.mark.unit
async def test_open_pr_ready_without_recovery_operation_is_blocked_before_feature_agent(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_open_pr_ready_workspace_without_recovery(factory)

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    post_agent_git_calls = [
        call.args
        for call in fake.calls
        if call.args
        and call.args[0] == "git"
        and any(token in call.args for token in {"add", "commit", "rev-list", "merge-base"})
    ]
    assert post_agent_git_calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert ws.failure_reason == "infrastructure_failure"
    assert ws.failure_message == "open PR exists; monitor recovery required"
    assert any(
        event.event_type == "workspace.pr_reexecution_blocked"
        and event.reason_code == "PR_REEXECUTION_GUARD"
        and event.payload
        == {
            "pr_number": 9,
            "pr_url": "https://github.com/x/y/pull/9",
            "status": WorkspaceStatus.running.value,
        }
        for event in events
    )
    assert any(
        event.event_type == "workspace.state_changed"
        and event.reason_code == "PR_REEXECUTION_GUARD"
        and event.old_state == WorkspaceStatus.running.value
        and event.new_state == WorkspaceStatus.failed.value
        for event in events
    )


@pytest.mark.unit
async def test_open_pr_guard_uses_fresh_recovery_operation_after_claim(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_open_pr_ready_workspace_without_recovery(factory)
    original_claim_ready = executor._claim_ready

    async def _claim_then_insert_recovery(
        workspace_id: str,
        **kwargs: Any,
    ) -> WorkspaceModel | None:
        ws = await original_claim_ready(workspace_id, **kwargs)
        assert ws is not None
        async with factory() as s:
            await OperationRepository(s).create(
                workspace_id=workspace_id,
                operation_type=OperationType.validate,
                payload={
                    "source": "pr_monitor",
                    "action": "validate_only",
                    "requested_action": "validate",
                    "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                    "recovery_mode": "validate_only",
                    "pr_number": 9,
                    "pr_url": "https://github.com/x/y/pull/9",
                },
                idempotency_key=f"pr_monitor:validate_only:{workspace_id}",
            )
            await s.commit()
        return ws

    executor._claim_ready = _claim_then_insert_recovery  # type: ignore[method-assign]
    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.completed.value
    assert all(event.event_type != "workspace.pr_reexecution_blocked" for event in events)
    recovery_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(recovery_ops) == 1
    assert recovery_ops[0].status == OperationStatus.succeeded.value


@pytest.mark.unit
async def test_setup_dependency_exhaustion_during_recovery_preserves_precise_monitor_reason(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    validation = _SetupFailureValidation(
        ValidationResult(commands=[_setup_dependency_exhausted_result(tmp_path)])
    )
    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        validation=validation,
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        resolved_profile={
            "name": "setup-retry",
            "phases": {"setup": ["uv sync --extra dev"]},
        },
    )

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)

    assert validation.calls == [("setup", "pre_agent")]
    assert fake.calls == []
    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert ws.failure_reason == "service_startup_failure"
    assert ws.failure_message == "profile setup failed: uv sync --extra dev"

    recovery_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(recovery_ops) == 1
    recovery_op = recovery_ops[0]
    assert recovery_op.status == OperationStatus.failed.value
    assert recovery_op.error_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert recovery_op.result == {"reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE}

    failed_events = [
        event
        for event in events
        if event.event_type == "workspace.state_changed"
        and event.new_state == WorkspaceStatus.failed.value
    ]
    assert failed_events
    terminal_event = failed_events[0]
    assert terminal_event.reason_code == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert terminal_event.payload is not None
    assert terminal_event.payload["reason_code"] == SETUP_DEPENDENCY_NETWORK_FAILURE
    assert terminal_event.payload["details"]["package"] == "docker==7.1.0"


@pytest.mark.unit
async def test_generic_setup_failure_during_recovery_preserves_monitor_setup_reason(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    validation = _SetupFailureValidation(
        ValidationResult(commands=[_generic_setup_failure_result(tmp_path)])
    )
    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        validation=validation,
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        resolved_profile={
            "name": "setup-failure",
            "phases": {"setup": ["./scripts/setup-local.sh"]},
        },
    )

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)

    assert validation.calls == [("setup", "pre_agent")]
    assert fake.calls == []
    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert ws.failure_reason == "service_startup_failure"
    assert ws.failure_message == "profile setup failed: ./scripts/setup-local.sh"

    recovery_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(recovery_ops) == 1
    recovery_op = recovery_ops[0]
    assert recovery_op.status == OperationStatus.failed.value
    assert recovery_op.error_code == "MONITOR_RECOVERY_SETUP_FAILED"
    assert recovery_op.result == {"reason_code": "MONITOR_RECOVERY_SETUP_FAILED"}


@pytest.mark.unit
async def test_runtime_ownership_repair_runs_before_recovery_setup(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    validation = _RecordingSetupFailureValidation(
        ValidationResult(commands=[_generic_setup_failure_result(tmp_path)]),
        events,
    )
    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        validation=validation,
    )
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    async def _repair(
        *,
        logger: Any,
        reason_code: str,
        event_name: str,
        **kwargs: Any,
    ) -> bool:
        events.append("repair")
        assert kwargs["workspace_id"] == ws_id
        assert kwargs["worktree_path"] == _test_worktrees_root(factory) / ws_id
        assert kwargs["reason"] == "profile_setup"
        assert reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        assert event_name == "executor.agent_runtime_ownership_repair_failed"
        assert logger is not None
        return True

    monkeypatch.setattr(
        "awf.control.executor.execution_flow.repair_agent_runtime_ownership",
        _repair,
    )

    await executor.execute(ws_id)

    assert events == ["repair", "setup"]


@pytest.mark.unit
async def test_runtime_ownership_repair_failure_blocks_recovery_setup(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation = _SetupFailureValidation(
        ValidationResult(commands=[_generic_setup_failure_result(tmp_path)])
    )
    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        validation=validation,
    )
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    async def _repair(*, logger: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(
        "awf.control.executor.execution_flow.repair_agent_runtime_ownership",
        _repair,
    )

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)

    assert validation.calls == []
    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert ws.failure_reason == "infrastructure_failure"
    assert ws.failure_message == "agent runtime ownership repair failed before profile setup"
    recovery_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(recovery_ops) == 1
    assert recovery_ops[0].status == OperationStatus.failed.value
    assert recovery_ops[0].error_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    assert recovery_ops[0].result == {
        "reason_code": AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    }
