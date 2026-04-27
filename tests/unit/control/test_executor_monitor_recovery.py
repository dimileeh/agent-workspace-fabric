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

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populates registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    _pending_monitor_recovery,
)
from awf.db.base import Base
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner

from .executor_paths import _test_worktree_path, _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


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
) -> WorkspaceExecutor:
    compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
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
    )


async def _seed_ready_workspace_with_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_url: str = "https://github.com/x/y/pull/1",
    pr_number: int = 1,
    create_worktree: bool = True,
    recovery_mode: str = "validate_only",
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
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
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
        await OperationRepository(s).create(
            workspace_id=ws.id,
            operation_type=OperationType.validate,
            payload={
                "source": "pr_monitor",
                "reason": "validation_insufficient_tier",
                "recovery_mode": recovery_mode,
            },
        )
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


def _all_adapter_args(fake: FakeCommandRunner) -> list[list[str]]:
    """Every `docker compose exec ... codex ...` invocation."""
    return [c.args for c in fake.calls if "exec" in c.args and "codex" in c.args]


def _all_adapter_prompts(fake: FakeCommandRunner) -> str:
    """Concatenate every adapter prompt invocation into a single string for substring search."""
    return "\n".join(" ".join(args) for args in _all_adapter_args(fake))


@pytest.mark.unit
def test_pending_monitor_recovery_predicate_returns_payload_when_pending() -> None:
    """The predicate must surface the recovery payload when an active
    pr_monitor operation exists, and return ``None`` otherwise."""

    class _FakeOperation:
        def __init__(self, *, status: str, payload: dict[str, object] | None) -> None:
            self.status = status
            self.payload = payload
            self.type = OperationType.validate.value

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
    operator = _FakeOperation(
        status=OperationStatus.pending.value,
        payload={"source": "operator"},
    )

    assert _pending_monitor_recovery(_FakeWorkspace([pending])) == pending.payload
    assert _pending_monitor_recovery(_FakeWorkspace([running])) == running.payload
    assert _pending_monitor_recovery(_FakeWorkspace([succeeded])) is None
    assert _pending_monitor_recovery(_FakeWorkspace([operator])) is None
    assert _pending_monitor_recovery(_FakeWorkspace([])) is None


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
    plan_path = (
        _test_worktree_path(factory, ws_id) / "docs" / "awf-plans" / f"{ws_id}.md"
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = "# pre-existing plan content — must survive monitor recovery\n"
    plan_path.write_text(sentinel, encoding="utf-8")

    # Recovery skips Step 1/1b. Validation runs once and passes; push +
    # PR update is a no-op against the existing PR URL.
    fake.queue_result(returncode=0, stdout="tests ok")  # validation
    _queue_push_and_pr(fake, pr_url="https://github.com/x/y/pull/1")

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
        assert ws is not None
        assert ws.status in {
            WorkspaceStatus.completed.value,
            WorkspaceStatus.monitoring_pr.value,
        }
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

    fake.queue_result(returncode=0, stdout="tests ok")  # validation
    _queue_push_and_pr(fake)

    await executor.execute(ws_id)

    async with factory() as s:
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(pr_monitor_ops) == 1
    op = pr_monitor_ops[0]
    assert op.status == OperationStatus.succeeded.value
    assert isinstance(op.result, dict)
    assert "validation_run_id" in op.result
    assert op.started_at is not None
    assert op.finished_at is not None
    assert op.started_at < op.finished_at


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
async def test_executor_normal_path_unchanged_when_no_recovery_op(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Regression guard: workspaces without a pr_monitor recovery op
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

    plan_path = (
        _test_worktree_path(factory, ws_id) / "docs" / "awf-plans" / f"{ws_id}.md"
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# pre-existing plan\n", encoding="utf-8")
    plan_mtime_before = plan_path.stat().st_mtime

    fake.queue_result(returncode=0, stdout="tests ok")  # validation
    _queue_push_and_pr(fake)

    await executor.execute(ws_id)

    prompts = _all_adapter_prompts(fake)
    assert "## Planning phase" not in prompts
    assert "## Execution phase" not in prompts
    assert "## Conformance phase" not in prompts
    assert plan_path.read_text(encoding="utf-8") == "# pre-existing plan\n"
    assert plan_path.stat().st_mtime == plan_mtime_before
