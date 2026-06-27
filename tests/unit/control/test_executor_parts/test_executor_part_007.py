"""Executor tests with FakeCommandRunner + PostgreSQL.

Each test drives one workspace through the full pipeline with canned
subprocess output. The single runner handles all compose/adapter/pr calls
since each call is distinguishable by its argv.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    agent_service_recovery,
)
from awf.control.executor import planning_artifacts as _planning_artifacts
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.repositories import (
    OperationRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.planning import (
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
)
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.pr_monitor_operations import (
    build_monitor_operation_payload,
    monitor_operation_idempotency_key,
)
from awf.runtime.validation import (
    ValidationRunner,
)
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
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
        ),
    )


def _queue_pre_push_diagnostics(fake: FakeCommandRunner, *, head: str = "deadbeef01") -> None:
    """Queue executor's committed-diff policy check plus the three canned
    git results ``PullRequestCreator`` reads for its pre-push diagnostic
    log line (``rev-parse HEAD``, ``rev-parse --abbrev-ref HEAD``,
    ``git log origin/<base>..HEAD``).

    Every test that drives the executor through the PR-creation step
    must call this immediately before queueing the ``git push`` result,
    because pr_creator now logs worktree state before pushing (added
    after the T39 incident where a ``gh pr create`` rejected with "No
    commits between development and awf/ws_...". The diagnostic block
    captures the local branch state so we can tell a bad-commit
    scenario apart from a stale worktree). These queued values are
    realistic enough that the log line reads sanely if a test prints
    captured output.
    """
    fake.queue_result(
        returncode=0, stdout="src/fix.py\n"
    )  # final plan-only gate: committed base..HEAD --name-only
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # committed base..HEAD diff
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref
    fake.queue_result(returncode=0, stdout="abc1234 commit\n")  # log ahead-of-base


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


def _created_pr_body(fake: FakeCommandRunner) -> str:
    create_call = next(call.args for call in fake.calls if call.args[:3] == ["gh", "pr", "create"])
    return create_call[create_call.index("--body") + 1]


def _json_value(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def _adapter_prompt_from_call(call: Any) -> str:
    input_bytes = call.input_bytes
    assert input_bytes is not None
    return input_bytes.decode()


def _adapter_prompt_calls(fake: FakeCommandRunner) -> list[tuple[int, str]]:
    return [
        (index, _adapter_prompt_from_call(call))
        for index, call in enumerate(fake.calls)
        if call.args[:2] == ["docker", "compose"]
        and "codex" in call.args
        and call.input_bytes is not None
    ]


def _adapter_prompts(fake: FakeCommandRunner) -> list[str]:
    return [prompt for _, prompt in _adapter_prompt_calls(fake)]


def _record_deposit_vs_mark_order(
    executor: WorkspaceExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Spy on planning-artifact deposits and FAILED-status marks, recording
    their relative order.

    The console keys its artifact refetch on the workspace ``updated_at``
    (TaskArtifactsSection ``refreshKey``); ``_mark_failed`` bumps ``updated_at``
    when it publishes the terminal FAILED status, but the filesystem deposit
    does not touch the row. If a deposit ran *after* the mark, a poll could
    observe the new ``updated_at`` in the window before the copy, record an
    empty artifact list, then never refetch — hiding the Plan/Validation
    controls. Every failure handler must therefore deposit BEFORE marking
    FAILED. The returned list records ``"deposit"``/``"mark_failed"`` in call
    order so a test can assert the deposit lands first.
    """
    order: list[str] = []
    real_deposit = _planning_artifacts._deposit_planning_artifacts_best_effort

    def _spy_deposit(*args: Any, **kwargs: Any) -> None:
        order.append("deposit")
        real_deposit(*args, **kwargs)

    monkeypatch.setattr(
        _planning_artifacts, "_deposit_planning_artifacts_best_effort", _spy_deposit
    )

    real_mark = executor._mark_failed

    async def _spy_mark(**kwargs: Any) -> None:
        order.append("mark_failed")
        await real_mark(**kwargs)

    monkeypatch.setattr(executor, "_mark_failed", _spy_mark)
    return order


async def _insert_validate_handoff_recovery_operation(
    factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    operation_id: str,
    requested_tier: int | None = None,
    conformance_overrides: Mapping[str, object] | None = None,
    created_at: datetime | None = None,
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        pr_number = 225
        source_head_sha = "deadbeef01"
        remote_branch = workspace.branch_name or f"awf/{workspace_id}"
        reason = "planning_conformance_requires_awf_validation"
        workspace.pr_number = pr_number
        workspace.pr_url = f"https://github.com/dimileeh/aira-agent/pull/{pr_number}"
        workspace.monitor_last_commit_sha = source_head_sha
        workspace.remote_push_branch = remote_branch
        conformance_payload: dict[str, object] = {
            "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
            "summary": "AWF validation evidence is required before conformance can pass.",
            "gaps": ["AWF-owned validation evidence is missing for the pytest gate."],
        }
        if conformance_overrides:
            conformance_payload.update(conformance_overrides)
        payload = build_monitor_operation_payload(
            workspace=workspace,
            action="validate_only",
            requested_action="validate",
            reason=reason,
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
            pr_number=pr_number,
            source_head_sha=source_head_sha,
            source_base_sha=workspace.base_commit,
            target_branch=workspace.branch_base,
            remote_branch=remote_branch,
            recovery_mode="validate_only",
            stale_reason=reason,
            extra={"conformance": conformance_payload},
        )
        if requested_tier is not None:
            payload["requested_tier"] = requested_tier
        await session.execute(
            text(
                """
                INSERT INTO operations (
                    id,
                    workspace_id,
                    type,
                    status,
                    payload,
                    idempotency_key,
                    created_at
                )
                VALUES (
                    :operation_id,
                    :workspace_id,
                    'validate',
                    'pending',
                    CAST(:payload AS JSON),
                    :idempotency_key,
                    :created_at
                )
                """
            ),
            {
                "operation_id": operation_id,
                "workspace_id": workspace_id,
                "payload": json.dumps(payload),
                "idempotency_key": monitor_operation_idempotency_key(
                    workspace_id=workspace_id,
                    action="validate_only",
                    pr_number=pr_number,
                    reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
                    source_head_sha=source_head_sha,
                    source_base_sha=workspace.base_commit,
                ),
                "created_at": created_at or datetime.now(UTC),
            },
        )
        await session.commit()


async def _seed_ready_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent: str = "codex",
    test_commands: list[str] | None = None,
    requires_database: bool = False,
    compose_file_path: str | None = None,
    resolved_profile: dict | None = None,
    task_policy: dict | None = None,
    create_worktree: bool = True,
) -> str:
    """Insert a workspace already in the ``ready`` state for the executor to pick up."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="trivial",
            task_prompt="Add a docstring.",
            agent=agent,
            test_commands=test_commands or ["pytest -q"],
            requires_database=requires_database,
            resolved_profile=resolved_profile,
            task_policy=task_policy or {},
        )
        # Walk through the transitions: requested → provisioning → ready.
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = compose_file_path
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await s.commit()
        if create_worktree:
            (_test_worktrees_root(factory) / ws.id).mkdir(parents=True, exist_ok=True)
        return ws.id


async def _seed_running_worker_restart_recovery(
    factory: async_sessionmaker[AsyncSession],
    *,
    execution_claimed_by: str | None = None,
    execution_claim_expires_at: datetime | None = None,
    workspace_status: WorkspaceStatus = WorkspaceStatus.running,
) -> str:
    ws_id = await _seed_ready_workspace(factory)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(ws_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="TEST_RUNNING")
        if workspace_status in {WorkspaceStatus.validating, WorkspaceStatus.pushing}:
            await repo.transition(
                ws,
                to=WorkspaceStatus.validating,
                reason_code="TEST_VALIDATING",
            )
        if workspace_status == WorkspaceStatus.pushing:
            await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="TEST_PUSHING")
        ws.execution_claimed_by = execution_claimed_by
        ws.execution_claim_expires_at = execution_claim_expires_at
        await OperationRepository(s).create(
            workspace_id=ws_id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            payload={
                "source": "worker_restart",
                "recovery_mode": "validate_only",
            },
        )
        await s.commit()
    return ws_id


class TestPlanningArtifactDeposits:
    """Planning-artifact deposit-before-FAILED regression tests.

    Split out of :class:`TestHappyPathPart002` to keep each executor test
    module under the maintainability line limit; behavior is unchanged.
    """

    @pytest.mark.unit
    async def test_planning_failure_deposits_plan_and_conformance_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # When the initial planning loop fails (conformance unsatisfied with no
        # iteration budget) the executor marks the workspace FAILED and returns
        # before the post-validation deposit block. The plan + conformance
        # report the agent already wrote into the worktree must still be
        # surfaced into the served artifact dir so the console buttons can show
        # the failed report on the preserved-FAILED workspace.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "max_iterations": 0,
                },
            },
        )
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "needs_iteration", "gaps": ["add tests"]}',
            encoding="utf-8",
        )

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # initial execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(
            returncode=0,
            stdout='{"status":"needs_iteration","summary":"still short","gaps":["add tests"]}',
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                f"?? docs/awf-plans/{ws_id}.md\n"
                f"?? docs/awf-plans/{ws_id}.conformance.json\n"
                " M src/x.py\n"
            ),
        )
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "needs_iteration", "gaps": ["add tests"]}'
        )

    @pytest.mark.unit
    async def test_post_agent_commit_failure_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Planning succeeds (handoff) but the post-agent commit step fails — a
        # pre-commit hook (here a failing ``git add -A``) rejects the staged
        # changes. The executor marks the workspace FAILED and returns from the
        # ``_PostAgentCommitStepError`` handler, which is BEFORE the
        # post-validation deposit block. The plan + conformance report the agent
        # already wrote into the preserved-FAILED worktree must still be
        # surfaced into the served artifact dir so the console can show them.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

        handoff_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Only AWF validation evidence is missing.",
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        )

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # initial execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout=handoff_report)
        fake.queue_result(
            returncode=0,
            stdout=(
                f"?? docs/awf-plans/{ws_id}.md\n"
                f"?? docs/awf-plans/{ws_id}.conformance.json\n"
                " M src/x.py\n"
            ),
        )
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD post-iter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch (drift)
        fake.queue_result(returncode=1, stderr="permission denied")  # git add -A fails

        order = _record_deposit_vs_mark_order(executor, monkeypatch)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )
        # ``_mark_post_agent_commit_failed`` routes through ``_mark_failed``; the
        # deposit must precede it so the console's ``updated_at``-keyed refetch
        # always observes the artifacts on the preserved-FAILED workspace.
        assert order.index("deposit") < order.index("mark_failed")

    @pytest.mark.unit
    async def test_unexpected_commit_step_error_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        # Planning succeeds (handoff), the commit lands, but the post-commit
        # ``git rev-list --count`` raises an unexpected error. That falls into
        # the generic ``except Exception`` handler, which marks the workspace
        # FAILED (infrastructure) and returns BEFORE the post-validation deposit
        # block — the same gap the ``_PostAgentCommitStepError`` branch already
        # closes. The plan + conformance report the agent wrote into the
        # preserved-FAILED worktree must still reach the served artifact dir.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

        handoff_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Only AWF validation evidence is missing.",
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        )

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # initial execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout=handoff_report)
        fake.queue_result(
            returncode=0,
            stdout=(
                f"?? docs/awf-plans/{ws_id}.md\n"
                f"?? docs/awf-plans/{ws_id}.conformance.json\n"
                " M src/x.py\n"
            ),
        )
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD post-iter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch (drift)
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="src/x.py\n")  # cached diff --name-only
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=1, stderr="fatal: bad revision")  # rev-list count FAILS

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            # A non-missing-HEAD commit-step error is infrastructure, not agent.
            assert ws.failure_reason == "infrastructure_failure"

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )

    @pytest.mark.unit
    async def test_no_work_exit_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Planning succeeds (handoff) and writes the plan + conformance report
        # into the worktree, but the implementation produces no staged changes,
        # so the post-agent ``git rev-list --count`` returns 0. The no-work
        # branch marks the workspace FAILED (agent failure) and returns BEFORE
        # the post-validation deposit block. The plan + conformance report the
        # agent left in the preserved-FAILED worktree must still reach the
        # served artifact dir so the console can surface why the task failed.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

        handoff_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Only AWF validation evidence is missing.",
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        )

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # initial execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout=handoff_report)
        fake.queue_result(
            returncode=0,
            stdout=(
                f"?? docs/awf-plans/{ws_id}.md\n"
                f"?? docs/awf-plans/{ws_id}.conformance.json\n"
                " M src/x.py\n"
            ),
        )
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD post-iter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch (drift)
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="")  # cached diff --name-only: no staged paths
        fake.queue_result(returncode=0, stdout="0\n")  # rev-list count = 0 → no-work exit

        order = _record_deposit_vs_mark_order(executor, monkeypatch)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            # No commits on the feature branch is an agent failure.
            assert ws.failure_reason == "agent_failure"

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )
        # The deposit must land BEFORE the FAILED-status bump so the console's
        # ``updated_at``-keyed refetch always observes the artifacts.
        assert order.index("deposit") < order.index("mark_failed")

    @pytest.mark.unit
    async def test_agent_phase_cleanup_error_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A ``ComposeExecCleanupError`` raised while the agent/planning run is in
        # flight (e.g. the agent CLI times out and AWF cannot prove its in-
        # container process tree is gone) lands in the agent-phase
        # ``except ComposeExecCleanupError`` handler, which marks the workspace
        # FAILED (infrastructure) and returns BEFORE the post-validation deposit
        # block. The plan + conformance report the agent already wrote into the
        # preserved-FAILED worktree must still reach the served artifact dir, the
        # same guarantee the unexpected-error path already provides.
        from awf.common.compose_exec import ComposeExecCleanupError

        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

        async def _raise_cleanup_error(**_kwargs: Any) -> object:
            raise ComposeExecCleanupError(
                invocation_id="awf_agent_plan_cleanup",
                source="agent",
                label="plan",
                message="tagged process still running",
            )

        monkeypatch.setattr(
            executor,
            "_run_agent_task_with_optional_planning",
            _raise_cleanup_error,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "EXEC_PROCESS_CLEANUP_FAILED" in (ws.failure_message or "")

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )

    @pytest.mark.unit
    async def test_agent_service_recovery_exhaustion_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Agent compose-service recovery marks the workspace FAILED when restart
        # attempts are exhausted and returns ``agent_service_recovered=False`` to
        # the executor. The executor must still surface any partial plan and
        # conformance report already written in the preserved FAILED worktree.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

        async def _service_down(*_args: object, **_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)
        monkeypatch.setattr(
            executor._compose,
            "ensure_project_up",
            AsyncMock(),
        )

        def _timeout_error() -> AgentRunError:
            return AgentRunError(
                agent=AgentRuntime.codex,
                result=CommandResult(
                    returncode=124,
                    stdout="",
                    stderr='service "agent" is not running',
                ),
                reason_code="AGENT_IDLE_TIMEOUT",
                details={
                    "provider": "openai",
                    "model": "gpt-5",
                    "provider_recovery": {
                        "reason_code": "AGENT_IDLE_TIMEOUT",
                        "failure_type": "idle_timeout",
                        "failure_scope": "provider",
                        "failure_fingerprint": "provider-fingerprint",
                    },
                },
            )

        async def _timeout_agent_run(**_kwargs: Any) -> object:
            raise _timeout_error()

        monkeypatch.setattr(
            executor,
            "_run_agent_task_with_optional_planning",
            _timeout_agent_run,
        )
        order = _record_deposit_vs_mark_order(executor, monkeypatch)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )
        assert order.index("deposit") < order.index("mark_failed")

    @pytest.mark.unit
    async def test_agent_phase_unexpected_error_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An unexpected (non-git-HEAD) error raised while the agent/planning run
        # is in flight lands in the agent-phase generic ``except Exception``
        # handler, which marks the workspace FAILED (infrastructure) and returns
        # BEFORE the post-validation deposit block. The plan + conformance report
        # the agent already wrote into the preserved-FAILED worktree must still
        # reach the served artifact dir, mirroring the ComposeExecCleanupError
        # handler.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

        async def _raise_unexpected(**_kwargs: Any) -> object:
            raise RuntimeError("boom: unexpected agent-run failure")

        monkeypatch.setattr(
            executor,
            "_run_agent_task_with_optional_planning",
            _raise_unexpected,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "unexpected error during agent run" in (ws.failure_message or "")

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )

    @pytest.mark.unit
    async def test_agent_phase_failed_head_recovery_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A missing-HEAD git error raised during the agent/planning run whose
        # recovery FAILS lands in the agent-phase generic ``except Exception``
        # handler: ``_recover_missing_git_head_or_mark_failed`` marks the
        # workspace FAILED and the handler returns BEFORE the post-validation
        # deposit block. The plan + conformance report the agent already wrote
        # into the preserved-FAILED worktree must still reach the served artifact
        # dir, mirroring the ComposeExecCleanupError handler.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

        async def _raise_missing_head(**_kwargs: Any) -> object:
            raise RuntimeError("fatal: bad object HEAD: missing blob object")

        async def _recovery_fails(**kwargs: Any) -> bool:
            # Honor the real ``_recover_missing_git_head_or_mark_failed``
            # contract: a False return means recovery failed AND the workspace
            # was already marked FAILED.
            await executor._mark_failed(
                workspace_id=kwargs["workspace_id"],
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message="missing-HEAD recovery failed",
            )
            return False

        monkeypatch.setattr(
            executor,
            "_run_agent_task_with_optional_planning",
            _raise_missing_head,
        )
        monkeypatch.setattr(
            executor,
            "_recover_missing_git_head_or_mark_failed",
            _recovery_fails,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )

    @pytest.mark.unit
    async def test_missing_base_commit_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A ``ready`` workspace whose ``base_commit`` is missing is an upstream
        # invariant violation: the post-agent commit step marks the workspace
        # FAILED (infrastructure) and returns BEFORE the post-validation deposit
        # block. The plan + conformance report the agent already wrote into the
        # preserved-FAILED worktree must still reach the served artifact dir,
        # mirroring the agent-phase failure handlers above.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.base_commit = None
            await s.commit()

        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

        async def _agent_run_ok(**_kwargs: Any) -> None:
            # Successful agent/planning run (no failure) so execution reaches
            # the post-agent ``base_commit`` invariant check.
            return None

        monkeypatch.setattr(
            executor,
            "_run_agent_task_with_optional_planning",
            _agent_run_ok,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "base_commit" in (ws.failure_message or "")

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )

    @pytest.mark.unit
    async def test_post_agent_stale_status_skip_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the workspace transitions out of ``running`` concurrently (e.g. a
        # cancel) between the agent run and the post-agent commit step, the
        # ``_recheck_status`` guard skips the rest of execution and returns
        # BEFORE the post-validation deposit block. The plan + conformance
        # report the agent already wrote into the worktree must still reach the
        # served artifact dir, mirroring the other post-planning early returns.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )

        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

        async def _agent_run_cancels(**_kwargs: Any) -> None:
            # Successful agent/planning run, but a concurrent cancel moves the
            # workspace out of ``running`` so the post-agent ``_recheck_status``
            # guard skips the commit step and returns.
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(ws_id)
                assert ws is not None
                ws.status = WorkspaceStatus.cancelled.value
                await s.commit()

        monkeypatch.setattr(
            executor,
            "_run_agent_task_with_optional_planning",
            _agent_run_cancels,
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            # The executor backs off without overwriting the concurrent status.
            assert ws.status == WorkspaceStatus.cancelled.value

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )

    @pytest.mark.unit
    async def test_cancel_during_ollama_pull_skips_baseline_coverage(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The agent git-writability preflight and the OpenCode/Ollama model pull
        # can each run for many minutes (an absent-model pull is bounded only by
        # the pull deadline, up to ~30 minutes). If a cancel lands while the pull
        # is in flight, the executor must recheck the status BEFORE the
        # baseline-coverage preflight — which runs the profile coverage command —
        # so cancellation stops further work promptly, rather than only noticing
        # at the agent-run recheck after baseline coverage has already executed.
        ws_id = await _seed_ready_workspace(
            factory,
            agent="opencode",
            resolved_profile={
                "name": "covered",
                "phases": {"validate": ["pytest -q"]},
            },
        )

        async def _pull_then_cancel(*, workspace_id: str, ws: Any) -> bool:
            # Simulate a cancel arriving while the (potentially 30-minute) pull
            # runs: flip the workspace out of ``running`` and report success.
            async with factory() as s:
                row = await WorkspaceRepository(s).get(workspace_id)
                assert row is not None
                row.status = WorkspaceStatus.cancelled.value
                await s.commit()
            return True

        monkeypatch.setattr(executor, "_ensure_ollama_model_or_mark_failed", _pull_then_cancel)

        baseline_calls: list[str] = []

        async def _spy_baseline(**kwargs: Any) -> None:
            baseline_calls.append(str(kwargs.get("workspace_id")))

        monkeypatch.setattr(executor, "_run_baseline_coverage_preflight", _spy_baseline)

        agent_calls: list[str] = []

        async def _spy_agent(**_kwargs: Any) -> None:
            agent_calls.append("ran")

        monkeypatch.setattr(executor, "_run_agent_task_with_optional_planning", _spy_agent)

        await executor.execute(ws_id)

        # The recheck after the pull short-circuits: baseline coverage and the
        # agent run never start, and the executor leaves the concurrent cancel
        # intact instead of overwriting it.
        assert baseline_calls == []
        assert agent_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value

    @pytest.mark.unit
    async def test_cancel_during_baseline_coverage_skips_agent_run(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The baseline-coverage preflight runs the profile coverage command and
        # can take a while. A cancel landing while it executes must be caught by
        # the agent-run recheck that follows it, so the agent never starts and
        # the executor records the stale skip against the ``agent_run`` action
        # instead of overwriting the concurrent cancel.
        ws_id = await _seed_ready_workspace(
            factory,
            agent="opencode",
            resolved_profile={
                "name": "covered",
                "phases": {"validate": ["pytest -q"]},
            },
        )

        async def _ensure_ok(*, workspace_id: str, ws: Any) -> bool:
            del workspace_id, ws
            return True

        monkeypatch.setattr(executor, "_ensure_ollama_model_or_mark_failed", _ensure_ok)

        async def _coverage_then_cancel(*, workspace_id: str, **_kwargs: Any) -> None:
            # Simulate a cancel arriving while baseline coverage runs: flip the
            # workspace out of ``running`` after the preflight recheck passed.
            async with factory() as s:
                row = await WorkspaceRepository(s).get(workspace_id)
                assert row is not None
                row.status = WorkspaceStatus.cancelled.value
                await s.commit()

        monkeypatch.setattr(executor, "_run_baseline_coverage_preflight", _coverage_then_cancel)

        agent_calls: list[str] = []

        async def _spy_agent(**_kwargs: Any) -> None:
            agent_calls.append("ran")

        monkeypatch.setattr(executor, "_run_agent_task_with_optional_planning", _spy_agent)

        await executor.execute(ws_id)

        # The agent-run recheck short-circuits after baseline coverage: the agent
        # never starts and the stale skip is recorded against ``agent_run``.
        assert agent_calls == []
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value
            assert ws.events[-1].event_type == "workspace.stale_action_skipped"
            assert ws.events[-1].reason_code == "EXECUTOR_STALE_STATUS"
            assert ws.events[-1].payload["action"] == "agent_run"

    async def _queue_planning_through_post_agent_add(
        self,
        fake: FakeCommandRunner,
        ws_id: str,
        *,
        cached_diff_stdout: str,
    ) -> None:
        """Queue planning → handoff → post-agent ``git add`` / cached diff.

        Stops right after the post-agent ``git diff --cached --name-only`` so
        the caller can drive one of the post-agent policy gates that return
        before the post-validation deposit block.
        """
        handoff_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Only AWF validation evidence is missing.",
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        )
        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # initial execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout=handoff_report)
        fake.queue_result(
            returncode=0,
            stdout=(
                f"?? docs/awf-plans/{ws_id}.md\n"
                f"?? docs/awf-plans/{ws_id}.conformance.json\n"
                " M src/x.py\n"
            ),
        )
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD post-iter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch (drift)
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout=cached_diff_stdout)  # cached diff --name-only

    def _write_worktree_plan_artifacts(
        self,
        factory: async_sessionmaker[AsyncSession],
        ws_id: str,
    ) -> None:
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- do work\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "gaps": []}',
            encoding="utf-8",
        )

    def _assert_served_plan_artifacts(self, tmp_path: Path, ws_id: str) -> None:
        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )

    @pytest.mark.unit
    async def test_supply_chain_block_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Planning succeeds (handoff) and writes the plan + conformance report
        # into the worktree, but the post-agent supply-chain policy gate blocks
        # the staged output. That branch marks the workspace FAILED (policy
        # failure) and returns BEFORE the post-validation deposit block, so the
        # plan + conformance report the agent left in the preserved-FAILED
        # worktree must still reach the served artifact dir.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        self._write_worktree_plan_artifacts(factory, ws_id)
        await self._queue_planning_through_post_agent_add(
            fake, ws_id, cached_diff_stdout="src/x.py\n"
        )

        async def _blocked(**_kwargs: Any) -> Any:
            return SimpleNamespace(policy_blocked=True, findings=[])

        monkeypatch.setattr(executor, "_refresh_supply_chain_policy_for_workspace", _blocked)
        order = _record_deposit_vs_mark_order(executor, monkeypatch)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "policy_failure"
            failed_event = next(
                event
                for event in reversed(ws.events)
                if event.event_type == "workspace.state_changed"
                and event.new_state == WorkspaceStatus.failed.value
            )
            assert failed_event.reason_code == "SUPPLY_CHAIN_POLICY_BLOCKED"

        self._assert_served_plan_artifacts(tmp_path, ws_id)
        # The deposit must land BEFORE the FAILED-status bump so the console's
        # ``updated_at``-keyed refetch always observes the artifacts.
        assert order.index("deposit") < order.index("mark_failed")

    @pytest.mark.unit
    async def test_plan_only_post_agent_gate_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Planning succeeds and writes the plan + conformance report, but the
        # post-agent staged output is plan-only, so the PLAN_ONLY_OUTPUT gate
        # marks the workspace FAILED and returns BEFORE the post-validation
        # deposit block. The preserved worktree's plan + conformance report
        # must still reach the served artifact dir.
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        self._write_worktree_plan_artifacts(factory, ws_id)
        await self._queue_planning_through_post_agent_add(
            fake, ws_id, cached_diff_stdout=f"docs/awf-plans/{ws_id}.md\n"
        )
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (base..HEAD) empty

        order = _record_deposit_vs_mark_order(executor, monkeypatch)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            failed_event = next(
                event
                for event in reversed(ws.events)
                if event.event_type == "workspace.state_changed"
                and event.new_state == WorkspaceStatus.failed.value
            )
            assert failed_event.reason_code == "PLAN_ONLY_OUTPUT"

        self._assert_served_plan_artifacts(tmp_path, ws_id)
        # ``_fail_if_plan_only_paths`` routes through ``_mark_failed``; the
        # deposit must precede it so the console's ``updated_at``-keyed refetch
        # always observes the artifacts on the preserved-FAILED workspace.
        assert order.index("deposit") < order.index("mark_failed")

    @pytest.mark.unit
    async def test_quality_gate_block_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Planning succeeds and writes the plan + conformance report, but the
        # post-agent output changes a protected quality-gate file. That gate now
        # pauses the workspace into ``blocked`` (operator decision) and returns
        # BEFORE the post-validation deposit block, so the preserved worktree's
        # plan + conformance report must still reach the served artifact dir.
        from awf.control.executor import execution_flow as _execution_flow
        from awf.control.quality_gates import QualityGateViolation

        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        self._write_worktree_plan_artifacts(factory, ws_id)
        await self._queue_planning_through_post_agent_add(
            fake, ws_id, cached_diff_stdout="src/x.py\n"
        )

        async def _no_diffs(**_kwargs: Any) -> dict[str, Any]:
            return {}

        monkeypatch.setattr(executor, "_protected_file_diffs_for_staged_paths", _no_diffs)
        monkeypatch.setattr(
            _execution_flow,
            "find_protected_quality_gate_changes",
            lambda **_kwargs: [
                QualityGateViolation(
                    path="pyproject.toml",
                    protected_pattern="pyproject.toml",
                    reason="protected quality-gate file changed",
                )
            ],
        )

        # The deposit must land BEFORE the block-status bump so the console's
        # ``updated_at``-keyed refetch always observes the artifacts.
        order: list[str] = []
        real_deposit = _planning_artifacts._deposit_planning_artifacts_best_effort

        def _spy_deposit(*args: Any, **kwargs: Any) -> None:
            order.append("deposit")
            real_deposit(*args, **kwargs)

        monkeypatch.setattr(
            _planning_artifacts, "_deposit_planning_artifacts_best_effort", _spy_deposit
        )
        real_block = executor.enter_blocked_for_protected_violation

        async def _spy_block(**kwargs: Any) -> bool:
            order.append("block")
            return await real_block(**kwargs)

        monkeypatch.setattr(executor, "enter_blocked_for_protected_violation", _spy_block)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.blocked.value
            assert ws.block_reason_code == "QUALITY_GATE_POLICY_CHANGED"
            assert ws.block_resume_phase == "post_agent_commit"
            assert ws.block_epoch == 1

        self._assert_served_plan_artifacts(tmp_path, ws_id)
        assert order.index("deposit") < order.index("block")
