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

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PLAN_CONFORMANCE_UNSATISFIED,
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


class TestHappyPathPart002:
    @pytest.mark.unit
    async def test_planning_validation_handoff_unexpected_post_validation_error_finishes_validate_operation(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned-recovery",
                "planning": {
                    "required": True,
                    "plan_path": "docs/awf-plans/{workspace_id}.md",
                    "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
                    "max_iterations": 2,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )
        operation_id = "op_pv_unexpected_failed"
        await _insert_validate_handoff_recovery_operation(
            factory,
            workspace_id=ws_id,
            operation_id=operation_id,
        )

        async def fail_record_event(**_: object) -> None:
            raise RuntimeError("event persistence exploded")

        executor._record_post_validation_conformance_event = fail_record_event  # type: ignore[method-assign]

        report_path = f"docs/awf-plans/{ws_id}.conformance.json"
        satisfied_report = json.dumps(
            {
                "status": "satisfied",
                "summary": "implementation and validation evidence satisfy the plan",
                "gaps": [],
            }
        )

        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=satisfied_report)  # conformance-only rerun
        fake.queue_result(returncode=0, stdout=f"?? {report_path}\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            run = (
                (
                    await s.execute(
                        text(
                            """
                            SELECT status, reason_code
                            FROM validation_runs
                            WHERE workspace_id = :workspace_id
                            """
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .one()
            )
            operation = (
                (
                    await s.execute(
                        text(
                            """
                            SELECT status, error_code, error_message, result, finished_at
                            FROM operations
                            WHERE id = :operation_id
                            """
                        ),
                        {"operation_id": operation_id},
                    )
                )
                .mappings()
                .one()
            )

        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "post-validation conformance check failed" in (ws.failure_message or "")
        assert "event persistence exploded" in (ws.failure_message or "")
        assert run == {"status": "succeeded", "reason_code": "VALIDATION_OK"}
        assert operation["status"] == "failed"
        assert operation["error_code"] == "POST_VALIDATION_CONFORMANCE_FAILED"
        assert operation["finished_at"] is not None
        assert "event persistence exploded" in operation["error_message"]
        result = _json_value(operation["result"])
        assert result["reason_code"] == "POST_VALIDATION_CONFORMANCE_FAILED"
        assert result["validation_run_id"]

    @pytest.mark.unit
    async def test_planning_validation_handoff_uses_validation_fix_cycle_before_rerun(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE),
            validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
            pr_creator=PullRequestCreator(fake),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
                default_models={AgentRuntime.codex: "gpt-5"},
                max_validation_fix_passes=1,
            ),
        )
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        handoff_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Only AWF validation evidence is missing.",
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        )
        satisfied_report = json.dumps(
            {
                "status": "satisfied",
                "summary": "implementation and validation evidence satisfy the plan",
                "gaps": [],
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
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="src/x.py\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=1, stderr="pytest: failed")  # validation fails
        fake.queue_result(returncode=0, stdout="fixed tests")  # validation fix adapter
        fake.queue_result(returncode=0)  # fix git add
        fake.queue_result(returncode=0, stdout="tests/test_x.py\n")  # fix cached diff
        fake.queue_result(returncode=0)  # fix commit
        _queue_validation_head(fake, head="b" * 40)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation recovers
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=satisfied_report)  # conformance-only rerun
        fake.queue_result(
            returncode=0,
            stdout=f"?? docs/awf-plans/{ws_id}.conformance.json\n",
        )
        fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        adapter_prompts = _adapter_prompts(fake)
        fix_prompt_index = next(
            index
            for index, prompt in enumerate(adapter_prompts)
            if "Validation failed after your previous pass" in prompt
        )
        post_validation_conformance_index = max(
            index for index, prompt in enumerate(adapter_prompts) if "Conformance phase" in prompt
        )
        post_validation_prompt = adapter_prompts[post_validation_conformance_index]

        assert fix_prompt_index < post_validation_conformance_index
        assert "### Validation evidence" in post_validation_prompt
        assert "VALIDATION_OK" in post_validation_prompt
        assert "COMMAND_FAILED" not in post_validation_prompt
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            runs = (
                (
                    await s.execute(
                        text(
                            "SELECT status, reason_code FROM validation_runs "
                            "WHERE workspace_id = :workspace_id ORDER BY started_at"
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .all()
            )

        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        assert [run["status"] for run in runs] == ["failed", "succeeded"]
        assert [run["reason_code"] for run in runs] == ["COMMAND_FAILED", "VALIDATION_OK"]

    @pytest.mark.unit
    async def test_planning_validation_handoff_keeps_remaining_conformance_fix_iteration(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE),
            validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
            pr_creator=PullRequestCreator(fake),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
                default_models={AgentRuntime.codex: "gpt-5"},
                max_validation_fix_passes=0,
            ),
        )
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "max_iterations": 1},
                "phases": {"validate": ["pytest -q"]},
            },
        )
        report_path = f"docs/awf-plans/{ws_id}.conformance.json"
        handoff_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Only AWF validation evidence is missing.",
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        )
        post_validation_gap_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Validation passed, but the implementation still misses behavior.",
                "gaps": ["Add the behavior required by the saved plan."],
            }
        )
        satisfied_report = json.dumps(
            {
                "status": "satisfied",
                "summary": "implementation and validation evidence satisfy the plan",
                "gaps": [],
            }
        )

        fake.queue_result(returncode=0, stdout="")  # changed paths before planning
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning adapter
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # execution adapter
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout=handoff_report)  # conformance handoff
        fake.queue_result(
            returncode=0,
            stdout=(f"?? docs/awf-plans/{ws_id}.md\n?? {report_path}\n M src/x.py\n"),
        )
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # rev-parse HEAD post-iter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="src/x.py\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake, head="b" * 40)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=post_validation_gap_report)
        fake.queue_result(returncode=0, stdout=f"?? {report_path}\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD
        fake.queue_result(returncode=0, stdout="implemented missing behavior")  # fix adapter
        fake.queue_result(returncode=0)  # fix git add
        fake.queue_result(returncode=0, stdout="src/x.py\n")  # fix cached diff
        fake.queue_result(returncode=0)  # fix commit
        _queue_validation_head(fake, head="c" * 40)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation recovers
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout=f"{'c' * 40}\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=satisfied_report)
        fake.queue_result(returncode=0, stdout=f"?? {report_path}\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD
        _queue_pre_push_diagnostics(fake, head="c" * 40)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        adapter_prompts = _adapter_prompts(fake)
        fix_prompts = [
            prompt
            for prompt in adapter_prompts
            if "Validation failed after your previous pass" in prompt
        ]
        post_validation_conformance_prompts = [
            prompt
            for prompt in adapter_prompts
            if "Conformance phase" in prompt and "### Validation evidence" in prompt
        ]

        assert len(fix_prompts) == 1
        assert "post-validation plan conformance" in fix_prompts[0]
        assert [
            line
            for prompt in post_validation_conformance_prompts
            for line in prompt.splitlines()
            if line.startswith("Iteration: ")
        ] == ["Iteration: 1", "Iteration: 2"]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            runs = (
                (
                    await s.execute(
                        text(
                            "SELECT status, reason_code FROM validation_runs "
                            "WHERE workspace_id = :workspace_id ORDER BY started_at"
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .all()
            )

        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        assert [run["status"] for run in runs] == ["succeeded", "succeeded"]
        assert [run["reason_code"] for run in runs] == ["VALIDATION_OK", "VALIDATION_OK"]

    @pytest.mark.unit
    async def test_post_validation_conformance_fix_does_not_consume_validation_fix_budget(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE),
            validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
            pr_creator=PullRequestCreator(fake),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
                default_models={AgentRuntime.codex: "gpt-5"},
                max_validation_fix_passes=1,
            ),
        )
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "plan_path": "docs/awf-plans/{workspace_id}.md",
                    "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
                    "max_iterations": 3,
                    "enforce_plan_only_changes": True,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )
        report_path = f"docs/awf-plans/{ws_id}.conformance.json"
        handoff_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Only AWF validation evidence is missing.",
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": ["AWF-owned validation evidence is missing for pytest."],
            }
        )
        post_validation_gap_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Validation passed, but the implementation still misses the plan.",
                "gaps": ["Add the saved API behavior required by the plan."],
            }
        )
        satisfied_report = json.dumps(
            {
                "status": "satisfied",
                "summary": "implementation and validation evidence satisfy the plan",
                "gaps": [],
            }
        )

        fake.queue_result(returncode=0, stdout="")  # changed paths before planning
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning adapter
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # execution adapter
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout=handoff_report)  # conformance handoff
        fake.queue_result(
            returncode=0,
            stdout=(f"?? docs/awf-plans/{ws_id}.md\n?? {report_path}\n M src/x.py\n"),
        )
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # rev-parse HEAD post-iter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="src/x.py\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake, head="b" * 40)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=post_validation_gap_report)
        fake.queue_result(returncode=0, stdout=f"?? {report_path}\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD
        fake.queue_result(returncode=0, stdout="implemented conformance fix")  # fix adapter
        fake.queue_result(returncode=0)  # fix git add
        fake.queue_result(returncode=0, stdout="src/x.py\n")  # fix cached diff
        fake.queue_result(returncode=0)  # fix commit
        _queue_validation_head(fake, head="c" * 40)
        fake.queue_result(returncode=1, stderr="pytest: failed after conformance fix")
        fake.queue_result(returncode=0, stdout="fixed pytest")  # validation fix adapter
        fake.queue_result(returncode=0)  # validation fix git add
        fake.queue_result(returncode=0, stdout="tests/test_x.py\n")  # validation fix diff
        fake.queue_result(returncode=0)  # validation fix commit
        _queue_validation_head(fake, head="d" * 40)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation recovers
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout=f"{'d' * 40}\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=satisfied_report)  # conformance-only rerun
        fake.queue_result(returncode=0, stdout=f"?? {report_path}\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD
        _queue_pre_push_diagnostics(fake, head="d" * 40)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        adapter_prompts = _adapter_prompts(fake)
        fix_prompts = [
            prompt
            for prompt in adapter_prompts
            if "Validation failed after your previous pass" in prompt
        ]

        assert len(fix_prompts) == 2
        assert "post-validation plan conformance" in fix_prompts[0]
        assert "pytest -q" in fix_prompts[1]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            runs = (
                (
                    await s.execute(
                        text(
                            "SELECT status, reason_code FROM validation_runs "
                            "WHERE workspace_id = :workspace_id ORDER BY started_at"
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .all()
            )

        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        assert [run["status"] for run in runs] == ["succeeded", "failed", "succeeded"]
        assert [run["reason_code"] for run in runs] == [
            "VALIDATION_OK",
            "COMMAND_FAILED",
            "VALIDATION_OK",
        ]

    @pytest.mark.unit
    async def test_planning_profile_iterates_when_conformance_reports_gaps(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "max_iterations": 1,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # initial execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(  # compare says not done
            returncode=0,
            stdout='{"status":"needs_iteration","summary":"gap","gaps":["add tests"]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
        fake.queue_result(returncode=0, stdout="fixed gap")  # iteration execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(  # compare satisfied
            returncode=0,
            stdout='{"status":"satisfied","summary":"done","gaps":[]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 1 post
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="src/x.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        adapter_prompts = _adapter_prompts(fake)
        assert len(adapter_prompts) == 5
        assert "Iteration 1" in adapter_prompts[3]

    @pytest.mark.unit
    async def test_planning_mixed_validation_and_api_gap_stays_agent_owned(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "max_iterations": 1,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )
        mixed_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Validation evidence is missing and a real API gap remains.",
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": [
                    "AWF-owned validation evidence is missing.",
                    "Wire the API endpoint required by the plan.",
                ],
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
        fake.queue_result(returncode=0, stdout=mixed_report)
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
        fake.queue_result(returncode=0, stdout="fixed api")  # iteration execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(
            returncode=0,
            stdout='{"status":"satisfied","summary":"done","gaps":[]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 1 post
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="src/x.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        adapter_prompt_calls = _adapter_prompt_calls(fake)
        adapter_prompts = [prompt for _, prompt in adapter_prompt_calls]
        validation_call_index = next(
            index
            for index, call in enumerate(fake.calls)
            if "pytest -q" in call.args[-1] and "codex" not in call.args
        )
        iteration_prompt_index = next(
            index
            for index, prompt in adapter_prompt_calls
            if "## Execution phase" in prompt and "Iteration 1" in prompt
        )

        assert len(adapter_prompts) == 5
        assert iteration_prompt_index < validation_call_index
        async with factory() as s:
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id, limit=100)
        assert not any(event.reason_code == CONFORMANCE_REQUIRES_AWF_VALIDATION for event in events)

    @pytest.mark.unit
    async def test_planning_profile_failure_records_conformance_evidence_and_salvage(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
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
            assert ws.failure_reason == "agent_failure"
            assert ws.failure_message == (
                "plan conformance was not satisfied after 0 iteration(s): add tests"
            )
            failed_event = next(
                event
                for event in reversed(ws.events)
                if event.event_type == "workspace.state_changed"
                and event.new_state == WorkspaceStatus.failed.value
            )
            assert failed_event.reason_code == PLAN_CONFORMANCE_UNSATISFIED
            assert failed_event.payload is not None
            assert failed_event.payload["details"]["conformance"]["gaps"] == ["add tests"]
            assert (
                failed_event.payload["details"]["conformance"]["reason_code"]
                == PLAN_CONFORMANCE_UNSATISFIED
            )
            assert failed_event.payload["salvage"] == {
                "hint": "Workspace worktree and branch were preserved for salvage.",
                "worktree_path": str(_test_worktrees_root(factory) / ws_id),
                "branch_name": f"awf/{ws_id}",
                "remote_push_branch": f"awf/{ws_id}",
            }

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
    async def test_planning_profile_fails_when_plan_phase_changes_code(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "enforce_plan_only_changes": True},
            },
        )
        retry_calls: list[tuple[str, dict[str, Any]]] = []

        async def _fake_retry_workspace_row(
            session: AsyncSession,
            workspace_id: str,
            **kwargs: Any,
        ) -> Any:
            del session
            retry_calls.append((workspace_id, kwargs))
            return SimpleNamespace(new_workspace=SimpleNamespace(id="ws_retry"))

        from awf.control.executor import planning_ops as executor_planning_ops

        monkeypatch.setattr(
            executor_planning_ops,
            "retry_workspace_row",
            _fake_retry_workspace_row,
            raising=False,
        )

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan plus code")  # planning
        fake.queue_result(  # after planning
            returncode=0,
            stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/awf/oops.py\n",
        )
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "planning phase changed files outside" in (ws.failure_message or "")
            failed_event = next(
                event
                for event in reversed(ws.events)
                if event.event_type == "workspace.state_changed"
                and event.new_state == WorkspaceStatus.failed.value
            )
            assert failed_event.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
            assert failed_event.payload is not None
            assert failed_event.payload["reason_code"] == AGENT_PLAN_PHASE_SCOPE_VIOLATION
            scope = failed_event.payload["details"]["planning_scope"]
            assert scope["scope_phase"] == "planning"
            assert scope["required_paths"] == [f"docs/awf-plans/{ws_id}.md"]
            assert scope["offending_paths"] == ["src/awf/oops.py"]
            assert scope["recovery_strategy"] == "discard_and_replan"
            assert scope["salvage_policy"] == "explicit_salvage_required"
            assert failed_event.payload["salvage"] == {
                "hint": "Workspace worktree and branch were preserved for salvage.",
                "worktree_path": str(_test_worktrees_root(factory) / ws_id),
                "branch_name": f"awf/{ws_id}",
                "remote_push_branch": f"awf/{ws_id}",
            }

        assert not any("add" in call.args for call in fake.calls)
        assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls)
        assert not any("push" in call.args for call in fake.calls)
        assert not any(call.args[-1] == "pytest -q" for call in fake.calls)
        assert retry_calls == [(ws_id, {})]
