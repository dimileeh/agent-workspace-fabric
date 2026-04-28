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
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populates registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    _get_active_recovery_payload,
)
from awf.db.base import Base
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace as WorkspaceModel
from awf.db.repositories import OperationRepository, ValidationRunRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner

from .executor_paths import _test_worktree_path, _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


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
    pr_monitor_factory: Any = None,
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
        pr_monitor_factory=pr_monitor_factory,
    )


async def _seed_ready_workspace_with_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_url: str = "https://github.com/x/y/pull/1",
    pr_number: int = 1,
    create_worktree: bool = True,
    recovery_mode: str = "validate_only",
    source: str = "pr_monitor",
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
        await OperationRepository(s).create(
            workspace_id=ws.id,
            operation_type=OperationType.validate,
            payload={
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
            },
            idempotency_key=f"{source}:{recovery_mode}:{ws.id}",
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
    plan_path = (
        _test_worktree_path(factory, ws_id) / "docs" / "awf-plans" / f"{ws_id}.md"
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = "# pre-existing plan content — must survive monitor recovery\n"
    plan_path.write_text(sentinel, encoding="utf-8")

    # Recovery skips Step 1/1b. Validation runs once and passes; push +
    # PR update is a no-op against the existing PR URL.
    _queue_validation_head(fake)
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
        operation
        for operation in ops
        if operation.id not in {non_recovery.id, running_recovery.id}
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

    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation
    _queue_push_and_pr(fake)

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

    _queue_validation_head(fake)
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
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, recovery_mode="rebase_only"
    )

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
        if ("push" in c.args and "git" in c.args)
        or (c.args[:3] == ["gh", "pr", "create"])
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
    ws_id = await _seed_ready_workspace_with_recovery(factory, pr_url="https://github.com/x/y/pull/1")

    # Only validation should run; no push or PR creation commands.
    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_push_and_pr_create_calls(fake) == []

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status in {
            WorkspaceStatus.completed.value,
            WorkspaceStatus.monitoring_pr.value,
        }


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
        async def run(
            self, *, workspace_id: str, compose_project: str, compose_file: Path
        ) -> None:
            monitor_calls.append({"workspace_id": workspace_id, "compose_project": compose_project})

    def _monitor_factory(*_args: Any, **_kwargs: Any) -> _FakeMonitor:
        return _FakeMonitor()

    executor = _make_executor(
        fake=fake, factory=factory, tmp_path=tmp_path, pr_monitor_factory=_monitor_factory
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, pr_url="https://github.com/x/y/pull/1"
    )

    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert _all_push_and_pr_create_calls(fake) == []
    assert len(monitor_calls) == 1
    assert monitor_calls[0]["workspace_id"] == ws_id

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value


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
async def test_rebase_only_recovery_rebases_pushes_and_skips_pr_recreate(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Rebase-only recovery updates the existing PR branch but does not
    recreate the PR."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, recovery_mode="rebase_only"
    )

    _queue_rebase_recovery(fake)
    _queue_validation_head(fake, head="c" * 40)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls)
    assert any(
        call.args[0] == "git" and "push" in call.args and "--force-with-lease" in call.args
        for call in fake.calls
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status in {
            WorkspaceStatus.completed.value,
            WorkspaceStatus.monitoring_pr.value,
        }


@pytest.mark.unit
async def test_rebase_only_recovery_marks_operation_failed_when_recording_raises(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, recovery_mode="rebase_only"
    )

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
async def test_rebase_only_recovery_skips_rebase_when_target_already_merged(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """If an earlier SyncBase already merged the target branch into the
    PR branch, rebase recovery should record that refreshed head and move
    straight to Tier 2 validation instead of replaying commits again."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, recovery_mode="rebase_only"
    )

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
async def test_stale_callback_cancelled_blocks_recovery(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """If a workspace is cancelled after the executor claims it, the
    recovery path must stop and must NOT silently mark the recovery
    operation succeeded."""
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
            async with factory() as s:
                repo = WorkspaceRepository(s)
                ws = await repo.get(workspace_id)
                if ws is not None and ws.status == WorkspaceStatus.running.value:
                    await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="CANCELLED")
                    await s.commit()
        return await original_recheck(workspace_id, expected=expected, action=action, reason_code=reason_code)

    executor._recheck_status = _patched_recheck

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.cancelled.value
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(pr_monitor_ops) == 1
    # The operation must NOT be silently succeeded.
    assert pr_monitor_ops[0].status != OperationStatus.succeeded.value


@pytest.mark.unit
async def test_stale_callback_destroyed_blocks_recovery(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """If a workspace is destroyed after the executor claims it, the
    recovery path must stop and must NOT silently mark the recovery
    operation succeeded."""
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
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(workspace_id)
                if ws is not None and ws.status == WorkspaceStatus.running.value:
                    # Bypass state machine: the point is a stale callback
                    # on a destroyed workspace, not a valid transition.
                    await s.execute(
                        sa_update(WorkspaceModel)
                        .where(WorkspaceModel.id == workspace_id)
                        .values(status=WorkspaceStatus.destroyed.value)
                    )
                    await s.commit()
        return await original_recheck(workspace_id, expected=expected, action=action, reason_code=reason_code)

    executor._recheck_status = _patched_recheck

    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.destroyed.value
        ops = await OperationRepository(s).list_all(workspace_id=ws_id)
    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(pr_monitor_ops) == 1
    assert pr_monitor_ops[0].status != OperationStatus.succeeded.value


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
