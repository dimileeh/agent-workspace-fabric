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
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populates registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    _get_active_recovery_payload,
    _MonitorRebaseRecoveryError,
)
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
from awf.runtime.planning import (
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PLAN_CONFORMANCE_UNSATISFIED,
)
from awf.runtime.pr_creator import PullRequestCreator, PullRequestError
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine

from .executor_paths import _test_worktree_path, _test_worktrees_root

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
    fake.queue_result(returncode=0, stdout="deadbeef01\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref HEAD
    fake.queue_result(returncode=0, stdout="abc1234 work\n")  # log ahead-of-base
    fake.queue_result(returncode=0)  # git push
    fake.queue_result(returncode=0, stdout=pr_url)  # gh pr create


def _queue_existing_pr_push(fake: FakeCommandRunner, *, head: str = "deadbeef01") -> None:
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


def _all_adapter_prompts(fake: FakeCommandRunner) -> str:
    """Concatenate every adapter prompt invocation into a single string for substring search."""
    return "\n".join(" ".join(args) for args in _all_adapter_args(fake))


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
async def test_executor_normal_path_unchanged_when_no_recovery_op(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Regression guard: workspaces without a validate-only recovery op
    must continue running planning/agent/feature execution as before.
    """
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_no_recovery(factory)

    # Standard initial-execution sequence (mirrors test_executor.py).
    fake.queue_result(returncode=0, stdout="codex finished")  # adapter
    fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
    fake.queue_result(returncode=0)  # git add
    fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation
    _queue_push_and_pr(fake)

    await executor.execute(ws_id)

    # The feature task prompt must be present in adapter invocations
    # (this is the normal execution path).
    adapter_invocations = _all_adapter_args(fake)
    assert len(adapter_invocations) == 1
    prompts = _all_adapter_prompts(fake)
    assert _FEATURE_TASK_PROMPT in prompts

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value


def _all_push_and_pr_create_calls(fake: FakeCommandRunner) -> list[list[str]]:
    """Every git push or gh pr create invocation."""
    return [
        c.args
        for c in fake.calls
        if ("push" in c.args and "git" in c.args) or (c.args[:3] == ["gh", "pr", "create"])
    ]


@pytest.mark.unit
async def test_recovery_skips_push_when_pr_already_exists(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A workspace in recovery with an existing PR must NOT re-push or
    re-create the PR. The executor should skip the entire push/PR-creation
    path and transition directly back to monitoring_pr (or completed if
    no monitor is wired)."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    # Only validation should run; no push or PR creation commands.
    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_push_and_pr_create_calls(fake) == []

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


@pytest.mark.unit
async def test_recovery_skip_push_with_factory_resumes_monitor_runner(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Recovery with an existing PR and a pr_monitor_factory must transition
    to monitoring_pr AND immediately hand off to the monitor runner, matching
    the normal execution path (Step 4)."""
    monitor_calls: list[dict[str, Any]] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            monitor_calls.append({"workspace_id": workspace_id, "compose_project": compose_project})

    def _monitor_factory(*_args: Any, **_kwargs: Any) -> _FakeMonitor:
        return _FakeMonitor()

    executor = _make_executor(
        fake=fake, factory=factory, tmp_path=tmp_path, pr_monitor_factory=_monitor_factory
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    assert len(monitor_calls) == 1
    assert monitor_calls[0]["workspace_id"] == ws_id

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "RECOVERY_VALIDATION_OK"
            and event.old_state == WorkspaceStatus.validating.value
            and event.new_state == WorkspaceStatus.monitoring_pr.value
            for event in events
        )


@pytest.mark.unit
async def test_sync_feature_pr_recovery_runs_validation_before_monitor_handoff(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Adopted PR workspaces must honor monitor recovery operations.

    A ``sync_feature_pr`` workspace re-entering ``ready`` from the PR monitor
    must run the pending validate-only recovery before it hands the PR back to
    the monitor. Otherwise the validate operation remains pending forever and
    the monitor loops on ``RECOVERY_IN_PROGRESS``.
    """
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            del compose_project, compose_file
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    ws_id = await _seed_sync_feature_pr_ready_workspace_with_recovery(factory)

    _queue_validation_head(fake, head="d" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert _all_push_and_pr_create_calls(fake) == []
    assert monitor_calls == [ws_id]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    assert ws.monitor_last_commit_sha == "d" * 40
    assert ws.base_commit == "a" * 40
    recovery_ops = [
        op
        for op in ops
        if op.type == OperationType.validate.value
        and isinstance(op.payload, dict)
        and op.payload.get("source") == "pr_monitor"
    ]
    assert len(recovery_ops) == 1
    assert recovery_ops[0].status == OperationStatus.succeeded.value
    assert len(runs) == 1
    assert runs[0].workspace_head_sha == "d" * 40
    assert any(
        event.event_type == "workspace.state_changed"
        and event.reason_code == "RECOVERY_VALIDATION_OK"
        and event.old_state == WorkspaceStatus.validating.value
        and event.new_state == WorkspaceStatus.monitoring_pr.value
        for event in events
    )


@pytest.mark.unit
async def test_validate_only_recovery_pushes_existing_pr_after_fix_commit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """If validate-only recovery needs a fix pass, the validated local
    commit must be pushed back to the already-open PR before monitor handoff."""
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            del compose_project, compose_file
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        pr_url="https://github.com/x/y/pull/161",
        pr_number=161,
    )
    source_head = "d" * 40
    fixed_head = "e" * 40

    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=1, stderr="pytest: failed")  # initial validation fails
    fake.queue_result(returncode=0)  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/awf/onboarding.py\n")  # diff --cached
    fake.queue_result(returncode=0)  # git commit
    _queue_validation_head(fake, head=fixed_head)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation passes after fix
    _queue_existing_pr_push(fake, head=fixed_head)

    await executor.execute(ws_id)

    push_and_pr_calls = _all_push_and_pr_create_calls(fake)
    assert any(call[0] == "git" and "push" in call for call in push_and_pr_calls)
    assert not any(call[:3] == ["gh", "pr", "create"] for call in push_and_pr_calls)
    assert monitor_calls == [ws_id]

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    assert ws.monitor_last_commit_sha == fixed_head
    assert runs[-1].workspace_head_sha == fixed_head
    assert runs[-1].target_head_sha == fixed_head
    assert any(
        event.event_type == "workspace.audit.git_push" and event.reason_code == "PR_UPDATED"
        for event in events
    )


@pytest.mark.unit
async def test_sync_feature_pr_validate_only_recovery_pushes_adopted_pr_head(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Adopted PR recovery must update the real PR head, not the local
    feature-sync branch used only inside the workspace."""
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            del compose_project, compose_file
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    source_head = "d" * 40
    fixed_head = "e" * 40
    ws_id = await _seed_sync_feature_pr_ready_workspace_with_recovery(
        factory,
        pr_number=206,
        source_head_sha=source_head,
    )

    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=1, stderr="pytest: failed")  # initial validation fails
    fake.queue_result(returncode=0)  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="tests/integration/test_alembic_postgres.py\n")
    fake.queue_result(returncode=0)  # git commit
    _queue_validation_head(fake, head=fixed_head)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation passes after fix
    _queue_existing_pr_push(fake, head=fixed_head)

    await executor.execute(ws_id)

    push_calls = [
        call for call in _all_push_and_pr_create_calls(fake) if call[0] == "git" and "push" in call
    ]
    assert len(push_calls) == 1
    assert "HEAD:refs/heads/feature/existing-pr" in push_calls[0]
    assert f"feature-sync/{ws_id}" not in push_calls[0]
    assert not any(
        call[:3] == ["gh", "pr", "create"] for call in _all_push_and_pr_create_calls(fake)
    )
    assert monitor_calls == [ws_id]

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    assert ws.branch_name == f"feature-sync/{ws_id}"
    assert ws.remote_push_branch == "feature/existing-pr"
    assert ws.monitor_last_commit_sha == fixed_head
    assert runs[-1].workspace_head_sha == fixed_head
    assert runs[-1].target_head_sha == fixed_head
    assert any(
        event.event_type == "workspace.audit.git_push"
        and event.reason_code == "PR_UPDATED"
        and event.payload
        and event.payload["remote_branch"] == "feature/existing-pr"
        and event.payload["branch_name"] == f"feature-sync/{ws_id}"
        for event in events
    )


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["git push", "gh pr create"])
async def test_sync_feature_pr_push_error_audit_records_adopted_pr_head(
    operation: str,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
    )
    source_head = "d" * 40
    fixed_head = "e" * 40
    ws_id = await _seed_sync_feature_pr_ready_workspace_with_recovery(
        factory,
        pr_number=208,
        source_head_sha=source_head,
    )
    push_attempts: list[dict[str, Any]] = []

    async def fail_push_and_open(**kwargs: Any) -> None:
        push_attempts.append(kwargs)
        raise PullRequestError(
            operation=operation,
            returncode=128 if operation == "git push" else 1,
            stderr="remote rejected the adopted PR head",
            head_sha=fixed_head,
        )

    monkeypatch.setattr(executor._pr_creator, "push_and_open", fail_push_and_open)

    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=1, stderr="pytest: failed")  # initial validation fails
    fake.queue_result(returncode=0)  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="tests/integration/test_alembic_postgres.py\n")
    fake.queue_result(returncode=0)  # git commit
    _queue_validation_head(fake, head=fixed_head)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation passes after fix

    await executor.execute(ws_id)

    assert len(push_attempts) == 1
    assert push_attempts[0]["branch_name"] == f"feature-sync/{ws_id}"
    assert push_attempts[0]["remote_branch_name"] == "feature/existing-pr"

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
        pr_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type="workspace.audit.pr_created",
            limit=10,
        )

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    events = push_events + pr_events
    assert len(events) == (1 if operation == "git push" else 2)
    assert all(event.payload is not None for event in events)
    for event in events:
        assert event.payload["remote_branch"] == "feature/existing-pr"
        assert event.payload["branch_name"] == f"feature-sync/{ws_id}"
        assert event.payload["source_head_sha"] == fixed_head
    assert any(event.payload["outcome"] == "failed" for event in events)


@pytest.mark.unit
async def test_sync_feature_pr_validate_only_recovery_pushes_fork_head_repo(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Adopted fork PR recovery must update the fork branch, not origin."""
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            del compose_project, compose_file
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    source_head = "d" * 40
    fixed_head = "e" * 40
    ws_id = await _seed_sync_feature_pr_ready_workspace_with_recovery(
        factory,
        pr_number=207,
        head_repo_slug="contributor/aira-agent",
        source_head_sha=source_head,
    )

    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=1, stderr="pytest: failed")  # initial validation fails
    fake.queue_result(returncode=0)  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/awf/runtime/pr_creator.py\n")
    fake.queue_result(returncode=0)  # git commit
    _queue_validation_head(fake, head=fixed_head)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation passes after fix
    _queue_existing_pr_push(fake, head=fixed_head)

    await executor.execute(ws_id)

    push_calls = [
        call for call in _all_push_and_pr_create_calls(fake) if call[0] == "git" and "push" in call
    ]
    assert len(push_calls) == 1
    push_index = push_calls[0].index("push")
    assert "-u" not in push_calls[0]
    assert push_calls[0][push_index + 1] == "git@github.com:contributor/aira-agent.git"
    assert "HEAD:refs/heads/feature/existing-pr" in push_calls[0]
    assert "origin" not in push_calls[0][push_index + 1 :]
    assert f"feature-sync/{ws_id}" not in push_calls[0]
    assert not any(
        call[:3] == ["gh", "pr", "create"] for call in _all_push_and_pr_create_calls(fake)
    )
    assert monitor_calls == [ws_id]


@pytest.mark.unit
async def test_validate_only_recovery_zero_adapter_calls_on_clean_pass(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Even stricter than the existing skip-planning test: recovery must
    issue zero ``docker compose exec`` adapter invocations on a clean
    validation pass (no fix-cycle needed)."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    # Zero adapter calls of any kind — not planning, not execution, not
    # conformance, and not fix-cycle prompts.
    adapter_invocations = _all_adapter_args(fake)
    assert adapter_invocations == []

    # Note: validation legitimately issues ``docker compose exec`` for profile
    # phase commands; only the *agent adapter* (coding CLI) must be absent.


@pytest.mark.unit
async def test_validate_only_recovery_with_conformance_handoff_pushes_report_commit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        resolved_profile={
            "name": "planned-recovery",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
            },
            "phases": {"validate": ["pytest -q"]},
        },
        recovery_payload_overrides={
            "conformance": {
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "summary": "Recovery needs AWF-owned validation evidence.",
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        },
    )

    report_path = f"docs/awf-plans/{ws_id}.conformance.json"
    source_head = "d" * 40
    report_head = "f" * 40
    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=0, stdout="tests ok")
    fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
    fake.queue_result(returncode=0, stdout=f"{source_head}\n")  # conformance scope HEAD
    fake.queue_result(
        returncode=0,
        stdout='{"status":"satisfied","summary":"validated recovery","gaps":[]}',
    )
    fake.queue_result(
        returncode=0,
        stdout=f"?? {report_path}\n",
    )
    fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD
    _queue_post_validation_conformance_report_commit(fake, report_path)
    fake.queue_result(returncode=0, stdout=f"{report_head}\n")  # post-report HEAD
    fake.queue_result(returncode=0, stdout=f"src/awf/onboarding.py\n{report_path}\n")
    _queue_existing_pr_push(fake, head=report_head)

    await executor.execute(ws_id)

    adapter_args = _all_adapter_args(fake)
    assert len(adapter_args) == 1
    prompt = adapter_args[0][-1]
    assert "## Conformance phase" in prompt
    assert "## Planning phase" not in prompt
    assert "## Execution phase" not in prompt
    assert "Validation evidence" in prompt
    assert "VALIDATION_OK" in prompt
    assert "validation.01_validate.stdout" in prompt

    git_calls = [call.args for call in fake.calls if call.args and call.args[0] == "git"]
    assert any(call[-3:] == ["add", "--", report_path] for call in git_calls)
    assert any(
        "commit" in call
        and "awf: post-validation conformance report" in call
        and call[-1] == report_path
        for call in git_calls
    )
    assert any(call[0] == "git" and "push" in call for call in git_calls)
    assert not any(
        call[:3] == ["gh", "pr", "create"] for call in _all_push_and_pr_create_calls(fake)
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.completed.value
    assert ws.monitor_last_commit_sha == report_head
    assert runs[-1].workspace_head_sha == source_head
    assert runs[-1].target_head_sha == report_head
    assert any(
        event.event_type == "workspace.audit.git_push" and event.reason_code == "PR_UPDATED"
        for event in events
    )
    recovery_ops = [
        op
        for op in ops
        if op.type == OperationType.validate.value
        and isinstance(op.payload, dict)
        and op.payload.get("source") == "pr_monitor"
        and op.payload.get("recovery_mode") == "validate_only"
    ]
    assert len(recovery_ops) == 1
    assert recovery_ops[0].status == OperationStatus.succeeded.value


@pytest.mark.unit
async def test_rebase_only_recovery_with_conformance_handoff_pushes_report_commit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        recovery_mode="rebase_only",
        resolved_profile={
            "name": "planned-rebase-recovery",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
            },
            "phases": {"validate": ["pytest -q"]},
        },
        recovery_payload_overrides={
            "conformance": {
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "summary": "Rebased recovery needs AWF-owned validation evidence.",
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        },
    )

    report_path = f"docs/awf-plans/{ws_id}.conformance.json"
    rebased_head = "c" * 40
    report_head = "f" * 40
    _queue_rebase_recovery(fake)
    _queue_validation_head(fake, head=rebased_head)
    fake.queue_result(returncode=0, stdout="tests ok")
    fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
    fake.queue_result(returncode=0, stdout=f"{rebased_head}\n")  # conformance scope HEAD
    fake.queue_result(
        returncode=0,
        stdout='{"status":"satisfied","summary":"validated rebased recovery","gaps":[]}',
    )
    fake.queue_result(returncode=0, stdout=f"?? {report_path}\n")
    fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD
    _queue_post_validation_conformance_report_commit(fake, report_path)
    fake.queue_result(returncode=0, stdout=f"{report_head}\n")  # post-report HEAD
    fake.queue_result(returncode=0, stdout=f"src/awf/onboarding.py\n{report_path}\n")
    _queue_existing_pr_push(fake, head=report_head)

    await executor.execute(ws_id)

    git_push_calls = [
        call.args
        for call in fake.calls
        if call.args and call.args[0] == "git" and "push" in call.args
    ]
    assert any("--force-with-lease" in call for call in git_push_calls)
    assert any("--force-with-lease" not in call for call in git_push_calls)
    assert not any(
        call[:3] == ["gh", "pr", "create"] for call in _all_push_and_pr_create_calls(fake)
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.completed.value
    assert ws.monitor_last_commit_sha == report_head
    assert runs[-1].workspace_head_sha == rebased_head
    assert runs[-1].target_head_sha == report_head
    assert any(
        event.event_type == "workspace.audit.git_push" and event.reason_code == "REBASE_OK"
        for event in events
    )
    assert any(
        event.event_type == "workspace.audit.git_push" and event.reason_code == "PR_UPDATED"
        for event in events
    )


@pytest.mark.unit
async def test_validate_only_recovery_conformance_failure_fails_without_fix_loop(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        resolved_profile={
            "name": "planned-recovery",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
            },
            "phases": {"validate": ["pytest -q"]},
        },
        recovery_payload_overrides={
            "conformance": {
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "summary": "Recovery needs AWF-owned validation evidence.",
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        },
    )

    source_head = "d" * 40
    unsatisfied_report = (
        '{"status":"needs_iteration",'
        '"summary":"validation evidence still does not satisfy the plan",'
        '"reason_code":"PLAN_CONFORMANCE_VALIDATION_EVIDENCE_GAP",'
        '"gaps":["profile validation evidence is still insufficient"]}'
    )
    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=0, stdout="tests ok")
    fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
    fake.queue_result(returncode=0, stdout=f"{source_head}\n")  # conformance scope HEAD
    fake.queue_result(returncode=0, stdout=unsatisfied_report)
    fake.queue_result(returncode=0, stdout="")  # post-validation conformance after status
    fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD

    # These entries document the old, wasteful path: a synthetic validation
    # failure drove a fix prompt and a second full validation run. They must
    # remain unused.
    fake.queue_result(returncode=0, stdout="attempted fix")  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="")  # git diff --cached --name-only
    _queue_validation_head(fake, head=source_head)
    fake.queue_result(returncode=0, stdout="tests ok again")
    fake.queue_result(returncode=0, stdout="")
    fake.queue_result(returncode=0, stdout=f"{source_head}\n")
    fake.queue_result(returncode=0, stdout=unsatisfied_report)
    fake.queue_result(returncode=0, stdout="")
    fake.queue_result(returncode=0, stdout="")

    with structlog.testing.capture_logs() as captured:
        await executor.execute(ws_id)

    adapter_args = _all_adapter_args(fake)
    assert len(adapter_args) == 1
    assert "## Conformance phase" in adapter_args[0][-1]
    assert "Validation failed after your previous pass" not in adapter_args[0][-1]
    assert any(
        event.get("event") == "executor.post_validation_conformance_recovery_single_attempt"
        and event.get("workspace_id") == ws_id
        and event.get("recovery_mode") == "validate_only"
        and event.get("will_retry") is False
        for event in captured
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        runs = await ValidationRunRepository(s).list_for_workspace(ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert ws.failure_reason == "agent_failure"
    assert ws.failure_message is not None
    assert "post-validation plan conformance was not satisfied" in ws.failure_message
    assert len(runs) == 1
    assert runs[0].status == "succeeded"
    recovery_ops = [
        op
        for op in ops
        if op.type == OperationType.validate.value
        and isinstance(op.payload, dict)
        and op.payload.get("source") == "pr_monitor"
        and op.payload.get("recovery_mode") == "validate_only"
    ]
    assert len(recovery_ops) == 1
    assert recovery_ops[0].status == OperationStatus.failed.value
    assert recovery_ops[0].error_code == PLAN_CONFORMANCE_UNSATISFIED
    assert isinstance(recovery_ops[0].result, dict)
    assert recovery_ops[0].result.get("reason_code") == PLAN_CONFORMANCE_UNSATISFIED


@pytest.mark.unit
async def test_rebase_only_recovery_rebases_pushes_and_skips_pr_recreate(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Rebase-only recovery updates the existing PR branch but does not
    recreate the PR."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    _queue_rebase_recovery(fake)
    _queue_validation_head(fake, head="c" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls)
    assert any(
        call.args[0] == "git" and "push" in call.args and "--force-with-lease" in call.args
        for call in fake.calls
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
        assert ws is not None
        assert ws.status in {
            WorkspaceStatus.completed.value,
            WorkspaceStatus.monitoring_pr.value,
        }
    assert len(push_events) == 1
    assert push_events[0].reason_code == "REBASE_OK"
    assert push_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "executor",
        "source": "executor",
        "action": "rebase_recovery_push",
        "outcome": "succeeded",
        "reason_code": "REBASE_OK",
        "operation_id": push_events[0].payload["operation_id"],
        "operation_type": "rebase",
        "pr_number": 1,
        "pr_url": "https://github.com/x/y/pull/1",
        "source_head_sha": "c" * 40,
        "source_base_sha": "b" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{ws_id}",
        "branch_name": f"awf/{ws_id}",
        "evidence": {
            "previous_source_base_sha": "a" * 40,
            "previous_source_head_sha": "d" * 40,
            "rebased": True,
        },
    }


@pytest.mark.unit
async def test_rebase_only_recovery_push_failure_records_redacted_audit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=0)  # git rebase origin/<base>
    fake.queue_result(returncode=0, stdout="b" * 40 + "\n")  # rev-parse origin/<base>
    fake.queue_result(returncode=0, stdout="c" * 40 + "\n")  # rev-parse HEAD
    fake.queue_result(
        returncode=128,
        stderr=("fatal: unable to access https://user:ghp_should_not_persist@github.com/org/repo"),
    )

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert "ghp_should_not_persist" not in (ws.failure_message or "")
    assert "https://[redacted]@github.com/org/repo" in (ws.failure_message or "")
    assert len(push_events) == 1
    assert push_events[0].reason_code == "MONITOR_RECOVERY_REBASE_FAILED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "rebase_recovery_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["source_head_sha"] == "c" * 40
    assert push_events[0].payload["source_base_sha"] == "b" * 40
    assert push_events[0].payload["evidence"]["operation"] == "git push --force-with-lease"
    assert push_events[0].payload["evidence"]["returncode"] == 128
    assert "ghp_should_not_persist" not in repr(push_events[0].payload)
    assert "https://[redacted]@github.com/org/repo" in repr(push_events[0].payload)


@pytest.mark.unit
async def test_rebase_only_recovery_marks_operation_failed_when_recording_raises(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    async def fail_record_success(**_kwargs: object) -> None:
        raise RuntimeError("write exploded")

    monkeypatch.setattr(
        executor,
        "_record_rebase_recovery_success",
        fail_record_success,
    )
    _queue_rebase_recovery(fake)

    with pytest.raises(RuntimeError, match="write exploded"):
        await executor._run_monitor_rebase_recovery(
            workspace_id=ws_id,
            worktree_path=_test_worktrees_root(factory) / ws_id,
            base_branch="development",
            branch_name=f"awf/{ws_id}",
            remote_branch=f"awf/{ws_id}",
            reason="validation_insufficient_tier",
            recovery_payload={
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "pr_number": 1,
                "source_base_sha": "a" * 40,
                "source_head_sha": "d" * 40,
            },
        )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert len(rebase_ops) == 1
    assert rebase_ops[0].status == OperationStatus.failed.value
    assert rebase_ops[0].error_code == "MONITOR_RECOVERY_REBASE_FAILED"
    assert rebase_ops[0].error_message == "write exploded"
    assert isinstance(rebase_ops[0].result, dict)
    assert rebase_ops[0].result["reason_code"] == "MONITOR_RECOVERY_REBASE_FAILED"


@pytest.mark.unit
async def test_operator_rebase_operation_is_reused_and_failed_when_rebase_fails(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        recovery_mode="rebase_only",
        source="operator_api",
        operation_type=OperationType.rebase,
    )

    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=1, stderr="conflict on README.md")  # git rebase
    fake.queue_result(returncode=0)  # git rebase --abort

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert len(rebase_ops) == 1
    assert rebase_ops[0].payload is not None
    assert rebase_ops[0].payload["source"] == "operator_api"
    assert rebase_ops[0].status == OperationStatus.failed.value
    assert rebase_ops[0].started_at is not None
    assert rebase_ops[0].finished_at is not None
    assert rebase_ops[0].error_code == "MONITOR_RECOVERY_REBASE_FAILED"
    assert "conflict on README.md" in (rebase_ops[0].error_message or "")
    assert rebase_ops[0].result == {
        "status": "failed",
        "reason_code": "MONITOR_RECOVERY_REBASE_FAILED",
        "source_base_sha": "a" * 40,
        "source_head_sha": "d" * 40,
    }


@pytest.mark.unit
async def test_rebase_recovery_reuses_active_operation_with_extra_payload_context(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        recovery_mode="rebase_only",
        source="operator_api",
        operation_type=OperationType.rebase,
    )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        rebase_op = next(op for op in ops if op.type == OperationType.rebase.value)
        rebase_op.payload = {
            **dict(rebase_op.payload or {}),
            "candidate_id": "candidate-1",
            "log_stream_refs": {"monitor": "monitor.log"},
        }
        rebase_op_id = rebase_op.id
        await s.commit()

    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=1, stderr="conflict on README.md")  # git rebase
    fake.queue_result(returncode=0)  # git rebase --abort

    with pytest.raises(_MonitorRebaseRecoveryError):
        await executor._run_monitor_rebase_recovery(
            workspace_id=ws_id,
            worktree_path=_test_worktrees_root(factory) / ws_id,
            base_branch="development",
            branch_name=f"awf/{ws_id}",
            remote_branch=f"awf/{ws_id}",
            reason="validation_insufficient_tier",
            recovery_payload={
                "source": "operator_api",
                "recovery_mode": "rebase_only",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "pr_number": 1,
                "source_base_sha": "a" * 40,
                "source_head_sha": "d" * 40,
            },
        )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert [op.id for op in rebase_ops] == [rebase_op_id]
    assert rebase_ops[0].status == OperationStatus.failed.value
    assert rebase_ops[0].payload is not None
    assert rebase_ops[0].payload["candidate_id"] == "candidate-1"
    assert rebase_ops[0].payload["log_stream_refs"] == {"monitor": "monitor.log"}


@pytest.mark.unit
async def test_rebase_recovery_reuses_active_operation_with_partial_payload_identity(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        recovery_mode="rebase_only",
        source="operator_api",
        operation_type=OperationType.rebase,
    )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        rebase_op = next(op for op in ops if op.type == OperationType.rebase.value)
        rebase_op.payload = {
            **dict(rebase_op.payload or {}),
            "candidate_id": "candidate-1",
            "log_stream_refs": {"monitor": "monitor.log"},
        }
        rebase_op_id = rebase_op.id
        await s.commit()

    fake.queue_result(returncode=0)  # git fetch origin <base>
    fake.queue_result(returncode=0)  # git switch <branch>
    fake.queue_result(returncode=1)  # git merge-base --is-ancestor origin/<base> HEAD
    fake.queue_result(returncode=1, stderr="conflict on README.md")  # git rebase
    fake.queue_result(returncode=0)  # git rebase --abort

    with pytest.raises(_MonitorRebaseRecoveryError):
        await executor._run_monitor_rebase_recovery(
            workspace_id=ws_id,
            worktree_path=_test_worktrees_root(factory) / ws_id,
            base_branch="development",
            branch_name=f"awf/{ws_id}",
            remote_branch=f"awf/{ws_id}",
            reason="validation_insufficient_tier",
            recovery_payload={
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "pr_number": 1,
                "source_base_sha": "a" * 40,
                "source_head_sha": "d" * 40,
            },
        )

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert [op.id for op in rebase_ops] == [rebase_op_id]
    assert rebase_ops[0].status == OperationStatus.failed.value
    assert rebase_ops[0].payload is not None
    assert rebase_ops[0].payload["candidate_id"] == "candidate-1"
    assert rebase_ops[0].payload["log_stream_refs"] == {"monitor": "monitor.log"}


@pytest.mark.unit
async def test_rebase_only_recovery_skips_rebase_when_target_already_merged(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """If an earlier SyncBase already merged the target branch into the
    PR branch, rebase recovery should record that refreshed head and move
    straight to Tier 2 validation instead of replaying commits again."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory, recovery_mode="rebase_only")

    _queue_already_synced_rebase_recovery(fake)
    _queue_validation_head(fake, head="c" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    git_calls = [call.args for call in fake.calls if call.args and call.args[0] == "git"]
    assert not any("rebase" in call for call in git_calls)
    assert not any("push" in call for call in git_calls)
    assert any("merge-base" in call for call in git_calls)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.base_commit == "b" * 40
        assert ws.monitor_last_commit_sha == "c" * 40
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    rebase_ops = [op for op in ops if op.type == OperationType.rebase.value]
    assert len(rebase_ops) == 1
    assert rebase_ops[0].status == OperationStatus.succeeded.value
    assert rebase_ops[0].result == {
        "status": "succeeded",
        "reason_code": "REBASE_OK",
        "source_base_sha": "a" * 40,
        "source_head_sha": "d" * 40,
        "target_base_sha": "b" * 40,
        "target_head_sha": "c" * 40,
        "pushed": False,
        "rebased": False,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "final_status",
    [
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
        WorkspaceStatus.destroying,
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
    ],
)
async def test_stale_callback_terminal_status_blocks_recovery(
    final_status: WorkspaceStatus,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """If a workspace enters a callback-terminal state after executor claim,
    recovery must stop and close the monitor-created operation as ignored."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    original_recheck = executor._recheck_status

    async def _patched_recheck(
        workspace_id: str,
        *,
        expected: WorkspaceStatus,
        action: str,
        reason_code: str = "EXECUTOR_STALE_STATUS",
    ) -> bool:
        if action == "execute" and expected == WorkspaceStatus.running:
            await _force_workspace_status(factory, workspace_id, final_status)
        return await original_recheck(
            workspace_id,
            expected=expected,
            action=action,
            reason_code=reason_code,
        )

    executor._recheck_status = _patched_recheck

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == final_status.value
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id, limit=20)
    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(pr_monitor_ops) == 1
    assert pr_monitor_ops[0].status == OperationStatus.cancelled.value
    assert pr_monitor_ops[0].result == {
        "status": "ignored",
        "reason_code": "STALE_CALLBACK_IGNORED",
        "callback_source": "executor",
        "callback_action": "execute",
        "expected_status": WorkspaceStatus.running.value,
        "actual_status": final_status.value,
    }
    ignored_events = [
        event for event in events if event.event_type == "workspace.stale_callback_ignored"
    ]
    assert ignored_events[-1].reason_code == "STALE_CALLBACK_IGNORED"
    assert ignored_events[-1].payload == {
        "callback_source": "executor",
        "callback_action": "execute",
        "expected_status": WorkspaceStatus.running.value,
        "actual_status": final_status.value,
        "reason_code": "EXECUTOR_STALE_STATUS",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "final_status",
    [
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
        WorkspaceStatus.destroying,
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
    ],
)
async def test_stale_validation_callback_terminal_status_cancels_recovery_operation(
    final_status: WorkspaceStatus,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    monitor_calls: list[str] = []
    validation = _TerminalAfterSuccessfulValidation(factory, final_status)

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
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        validation=validation,
        pr_monitor_factory=lambda *_args: _Monitor(),
    )
    ws_id = await _seed_ready_workspace_with_recovery(factory)
    _queue_validation_head(fake)

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
        events = await WorkspaceEventRepository(s).list(workspace_id=ws_id, limit=30)
    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]

    assert validation.calls == [("setup", "pre_agent"), ("post_agent", "validate")]
    assert ws.status == final_status.value
    assert monitor_calls == []
    assert pr_monitor_ops[0].status == OperationStatus.cancelled.value
    assert pr_monitor_ops[0].result == {
        "status": "ignored",
        "reason_code": "STALE_CALLBACK_IGNORED",
        "callback_source": "executor",
        "callback_action": "validate",
        "expected_status": WorkspaceStatus.validating.value,
        "actual_status": final_status.value,
        "validation_run_id": pr_monitor_ops[0].result["validation_run_id"],
        "requested_tier": pr_monitor_ops[0].result["requested_tier"],
        "log_stream_refs": pr_monitor_ops[0].result["log_stream_refs"],
    }
    ignored_events = [
        event for event in events if event.event_type == "workspace.stale_callback_ignored"
    ]
    assert ignored_events[-1].payload == {
        "callback_source": "executor",
        "callback_action": "validate",
        "expected_status": WorkspaceStatus.validating.value,
        "actual_status": final_status.value,
        "reason_code": "STALE_CALLBACK_IGNORED",
    }


@pytest.mark.unit
async def test_executor_recovery_does_not_run_planning_when_planning_profile_required(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Even when the workspace's profile mandates planning, recovery
    must skip plan/execute/compare entirely. This is the strongest
    form of the "do not rewrite plan files" rule."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)

    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="recovery planned",
            task_prompt=_FEATURE_TASK_PROMPT,
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "plan_path": "docs/awf-plans/{workspace_id}.md",
                    "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
                    "max_iterations": 1,
                    "enforce_plan_only_changes": True,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = "https://github.com/x/y/pull/1"
        ws.pr_number = 1
        ws.remote_push_branch = ws.branch_name
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="X")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="RECOVERY_DISPATCH")
        await OperationRepository(s).create(
            workspace_id=ws.id,
            operation_type=OperationType.validate,
            payload={
                "source": "pr_monitor",
                "reason": "validation_insufficient_tier",
                "recovery_mode": "validate_only",
            },
        )
        await s.commit()
        ws_id = ws.id
        (_test_worktrees_root(factory) / ws_id).mkdir(parents=True, exist_ok=True)

    plan_path = _test_worktree_path(factory, ws_id) / "docs" / "awf-plans" / f"{ws_id}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# pre-existing plan\n", encoding="utf-8")
    plan_mtime_before = plan_path.stat().st_mtime

    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation
    _queue_push_and_pr(fake)

    await executor.execute(ws_id)

    prompts = _all_adapter_prompts(fake)
    assert "## Planning phase" not in prompts
    assert "## Execution phase" not in prompts
    assert "## Conformance phase" not in prompts
    assert plan_path.read_text(encoding="utf-8") == "# pre-existing plan\n"
    assert plan_path.stat().st_mtime == plan_mtime_before
