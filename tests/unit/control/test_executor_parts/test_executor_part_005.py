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
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import COMMAND_IDLE_TIMEOUT_REASON, FakeCommandRunner
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED
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


def _queue_post_validation_conformance_report_commit(
    fake: FakeCommandRunner, report_path: str
) -> None:
    fake.queue_result(returncode=0)  # git add report
    fake.queue_result(returncode=0, stdout=f"{report_path}\n")  # cached report diff
    fake.queue_result(returncode=0)  # commit refreshed report


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


class TestFailurePaths:
    @pytest.mark.unit
    async def test_agent_failure_with_no_work_marks_failed(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Agent exits non-zero AND left no file changes. Nothing to salvage →
        # workspace fails with agent_failure before validation runs.
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=2, stderr="codex: auth failed")  # adapter dies
        # Executor checks branch drift before the commit block
        # (rev-parse --abbrev-ref HEAD). Return the expected branch
        # name (awf/<ws_id>) to skip the recovery path.
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # abbrev-ref
        fake.queue_result(returncode=0)  # git add -A (no-op)
        fake.queue_result(returncode=0, stdout="")  # diff --cached empty
        fake.queue_result(returncode=0, stdout="0\n")  # rev-list = 0

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
        # Validation + PR never ran; 5 subprocess calls total (adapter
        # + drift-check + add + diff + rev-list).
        assert len(fake.calls) == 5

    @pytest.mark.unit
    async def test_applies_agent_scratch_excludes_before_agent_run(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The executor must hand the agent's checkout-local scratch paths to
        # the git-native exclude writer before the agent runs, so the
        # validation guard later treats them as ignored rather than dirty.
        import awf.control.executor.execution_flow as execution_flow

        calls: list[dict[str, Any]] = []

        async def _spy_apply(
            *,
            run_git: Any,
            worktree_path: Path,
            scratch_paths: tuple[str, ...],
            **kwargs: Any,
        ) -> bool:
            calls.append({"worktree_path": worktree_path, "scratch_paths": scratch_paths})
            return True

        monkeypatch.setattr(execution_flow, "apply_agent_scratch_excludes", _spy_apply)

        ws_id = await _seed_ready_workspace(factory, agent="claude_code")
        fake.queue_result(returncode=2, stderr="claude: boom")  # adapter dies
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # abbrev-ref
        fake.queue_result(returncode=0)  # git add -A (no-op)
        fake.queue_result(returncode=0, stdout="")  # diff --cached empty
        fake.queue_result(returncode=0, stdout="0\n")  # rev-list = 0

        await executor.execute(ws_id)

        assert len(calls) == 1
        assert calls[0]["scratch_paths"] == (".claude/worktrees/",)
        assert calls[0]["worktree_path"].name == ws_id

    @pytest.mark.unit
    async def test_cached_diff_git_error_is_treated_as_no_staged_paths(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # branch check
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(
            returncode=1,
            stdout="src/awf/foo.py\n",
            stderr="fatal: bad index",
        )  # cached diff command failed but returned staged-looking stdout
        fake.queue_result(returncode=0, stdout="")  # rev-list count

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert not any(call.args[:2] == ["git", "commit"] for call in fake.calls)

    @pytest.mark.unit
    async def test_rev_list_git_error_fails_infrastructure_path(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # branch check
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="")  # cached diff (no paths)
        fake.queue_result(returncode=1, stderr="fatal: bad object")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"

    @pytest.mark.unit
    async def test_agent_killed_with_uncommitted_work_is_salvaged(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Agent exits non-zero (e.g. claude_code SIGKILL 137 after long
        # session) but the worktree has uncommitted edits — the work IS
        # there, the CLI just didn't get to run its own final commit.
        # AWF must capture that work rather than throwing it away.
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=137, stderr="")  # adapter killed mid-session
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(
            returncode=0, stdout="tests/e2e/bff/tasks.spec.ts\n"
        )  # cached diff: real work
        fake.queue_result(returncode=0)  # git commit (AWF's auto-commit)
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-web/pull/999\n",
        )  # gh pr create

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.subphase == "validation"
            assert ws.last_activity_at is not None
            assert ws.pr_url == "https://github.com/dimileeh/aira-web/pull/999"

    @pytest.mark.unit
    async def test_validation_failure_marks_failed_with_reason(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """With the fix-cycle loop disabled (``max_validation_fix_passes=0``),
        a single validation failure still marks the workspace failed with
        the ``validation_failure`` reason — the pre-fix-cycle contract
        this test was originally written for."""
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
                default_models={
                    AgentRuntime.codex: "gpt-5",
                    AgentRuntime.claude_code: "sonnet",
                    AgentRuntime.gemini: "gemini-2.5-pro",
                },
                max_validation_fix_passes=0,
            ),
        )
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter ok
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached (non-empty)
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=1, stderr="pytest: 5 failed")  # validation fails

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "validation_failure"

    @pytest.mark.unit
    async def test_coverage_below_threshold_fails_validation_with_structured_reason(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
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
                default_models={
                    AgentRuntime.codex: "gpt-5",
                    AgentRuntime.claude_code: "sonnet",
                    AgentRuntime.gemini: "gemini-2.5-pro",
                },
                max_validation_fix_passes=0,
            ),
        )
        ws_id = await _seed_ready_workspace(
            factory,
            test_commands=[],
            resolved_profile={
                "name": "coverage-executor",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "strategy": {"final_gate": "coverage"},
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term",
                    },
                },
            },
        )
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO operations (
                        id,
                        workspace_id,
                        type,
                        status,
                        payload,
                        created_at
                    )
                    VALUES (
                        'op_validate_coverage',
                        :workspace_id,
                        'validate',
                        'pending',
                        '{"reason":"manual_validate"}',
                        :created_at
                    )
                    """
                ),
                {"workspace_id": ws_id, "created_at": datetime.now(UTC)},
            )
            await session.commit()

        fake.queue_result(
            returncode=1,
            stdout=(
                "Name        Stmts   Miss  Cover\n"
                "-------------------------------\n"
                "TOTAL         100     12    88%\n"
            ),
        )  # baseline coverage preflight
        fake.queue_result(returncode=0, stdout="codex finished")  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
        fake.queue_result(
            returncode=0,
            stdout=(
                "Name        Stmts   Miss  Cover\n"
                "-------------------------------\n"
                "TOTAL         100     13    87%\n"
            ),
        )  # coverage cmd

        await executor.execute(ws_id)

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            run = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT status, reason_code, log_stream_refs
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
                    await session.execute(
                        text(
                            """
                        SELECT status, error_code, result
                        FROM operations
                        WHERE id = 'op_validate_coverage'
                        """
                        )
                    )
                )
                .mappings()
                .one()
            )

        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == "validation_failure"
        assert "coverage" in (workspace.failure_message or "").lower()
        assert run["status"] == "failed"
        assert run["reason_code"] == "COVERAGE_BELOW_THRESHOLD"
        assert _json_value(run["log_stream_refs"])["coverage"] == {
            "provider": "python",
            "percent": 87.0,
            "minimum_percent": 99.0,
            "enforce": True,
            "status": "failed",
            "reason_code": "COVERAGE_BELOW_THRESHOLD",
            "baseline_percent": 88.0,
            "baseline_status": "failed",
            "baseline_reason_code": "COVERAGE_BELOW_THRESHOLD",
        }
        assert operation["status"] == "failed"
        assert operation["error_code"] == "COVERAGE_BELOW_THRESHOLD"
        assert _json_value(operation["result"])["coverage"] == {
            "provider": "python",
            "percent": 87.0,
            "minimum_percent": 99.0,
            "enforce": True,
            "status": "failed",
            "reason_code": "COVERAGE_BELOW_THRESHOLD",
            "baseline_percent": 88.0,
            "baseline_status": "failed",
            "baseline_reason_code": "COVERAGE_BELOW_THRESHOLD",
        }

    @pytest.mark.unit
    async def test_healthcheck_failure_records_validation_run_failure_and_event(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
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
                default_models={AgentRuntime.codex: "gpt-5"},
                max_validation_fix_passes=0,
            ),
        )
        ws_id = await _seed_ready_workspace(
            factory,
            test_commands=[],
            resolved_profile={
                "name": "healthcheck-executor",
                "phases": {"validate": ["pytest -q"]},
                "validation": {
                    "healthchecks": [
                        {
                            "name": "api",
                            "command": "curl -fsS http://api:8000/healthz",
                            "timeout_seconds": 0.001,
                            "interval_seconds": 0.001,
                        }
                    ]
                },
            },
        )

        fake.queue_result(returncode=0, stdout="codex finished")  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=7, stderr="connection refused")  # healthcheck

        await executor.execute(ws_id)

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            run = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT status, reason_code, commands
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
            events = [
                event
                for event in workspace.events
                if event.event_type == "workspace.health_check_failed"
            ]

        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == "health_check_failure"
        assert "health check api" in (workspace.failure_message or "")
        assert "validation.01_healthcheck.stderr" in (workspace.failure_message or "")
        assert run["status"] == "failed"
        assert run["reason_code"] == "HEALTHCHECK_COMMAND_FAILED"
        commands = _json_value(run["commands"])
        assert commands[0]["phase"] == "healthcheck"
        assert commands[0]["retry_count"] == 0
        assert len(events) == 1
        assert events[0].reason_code == "HEALTHCHECK_COMMAND_FAILED"
        assert events[0].payload == {
            "healthcheck_name": "api",
            "healthcheck_kind": "command",
            "target": "curl -fsS http://api:8000/healthz",
            "attempts": 1,
            "timeout_seconds": 0.001,
            "stream_ids": {
                "stdout": "validation.01_healthcheck.stdout",
                "stderr": "validation.01_healthcheck.stderr",
            },
        }
        assert not any("pytest -q" in call.args[-1] for call in fake.calls)

    @pytest.mark.unit
    async def test_push_failure_marks_failed_with_infrastructure_reason(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter ok
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation ok
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(
            returncode=128,
            stderr=(
                "remote: perm denied for https://user:ghp_should_not_persist@github.com/org/repo"
            ),
        )  # push fails

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "ghp_should_not_persist" not in (ws.failure_message or "")
            assert "https://[redacted]@github.com/org/repo" in (ws.failure_message or "")
            push_events = await WorkspaceEventRepository(s).list(
                workspace_id=ws_id,
                event_type="workspace.audit.git_push",
                limit=10,
            )
            assert len(push_events) == 1
            assert push_events[0].reason_code == "GIT_PUSH_FAILED"
            assert push_events[0].payload is not None
            assert push_events[0].payload["actor"] == "executor"
            assert push_events[0].payload["action"] == "git_push"
            assert push_events[0].payload["outcome"] == "failed"
            assert push_events[0].payload["reason_code"] == "GIT_PUSH_FAILED"
            assert push_events[0].payload["remote_branch"] == f"awf/{ws_id}"
            assert push_events[0].payload["evidence"] == {
                "operation": "git push",
                "returncode": 128,
                "error_message": ("remote: perm denied for https://[redacted]@github.com/org/repo"),
            }

    @pytest.mark.unit
    async def test_pr_create_failure_records_redacted_audit_after_successful_push(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="f\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)
        _queue_pre_push_diagnostics(fake, head="pushed-head")
        fake.queue_result(returncode=0)
        fake.queue_result(
            returncode=1,
            stderr=(
                "GraphQL failed for https://user:ghp_should_not_persist@github.com/org/repo "
                "Authorization: Bearer ghp_should_not_persist"
            ),
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "ghp_should_not_persist" not in (ws.failure_message or "")
            assert "https://[redacted]@github.com/org/repo" in (ws.failure_message or "")
            events = WorkspaceEventRepository(s)
            push_events = await events.list(
                workspace_id=ws_id,
                event_type="workspace.audit.git_push",
                limit=10,
            )
            pr_events = await events.list(
                workspace_id=ws_id,
                event_type="workspace.audit.pr_created",
                limit=10,
            )
        assert len(push_events) == 1
        assert push_events[0].payload is not None
        assert push_events[0].payload["outcome"] == "succeeded"
        assert push_events[0].payload["source_head_sha"] == "pushed-head"
        assert len(pr_events) == 1
        assert pr_events[0].reason_code == "PR_CREATE_FAILED"
        assert pr_events[0].payload is not None
        assert pr_events[0].payload["outcome"] == "failed"
        assert pr_events[0].payload["action"] == "pr_create"
        assert pr_events[0].payload["evidence"]["operation"] == "gh pr create"
        assert pr_events[0].payload["evidence"]["returncode"] == 1
        assert "ghp_should_not_persist" not in repr(pr_events[0].payload)
        assert "https://[redacted]@github.com/org/repo" in repr(pr_events[0].payload)

    @pytest.mark.unit
    async def test_transient_pr_create_exhaustion_records_retry_evidence(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        executor._pr_creator = PullRequestCreator(  # noqa: SLF001
            fake,
            pr_create_transient_max_retries=0,
            pr_create_transient_initial_backoff_seconds=0,
        )
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="f\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)
        _queue_pre_push_diagnostics(fake, head="pushed-head")
        fake.queue_result(returncode=0)
        fake.queue_result(
            returncode=1,
            stderr='Post "https://api.github.com/graphql": dial tcp: i/o timeout',
        )
        fake.queue_result(returncode=0, stdout="[]")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            pr_events = await WorkspaceEventRepository(s).list(
                workspace_id=ws_id,
                event_type="workspace.audit.pr_created",
                limit=10,
            )

        assert len(pr_events) == 1
        evidence = pr_events[0].payload["evidence"]
        assert evidence["operation"] == "gh pr create"
        assert evidence["returncode"] == 1
        assert evidence["details"]["strategy"] == "transient_retry_exhausted"
        assert evidence["details"]["attempts"] == 1
        assert evidence["details"]["max_retries"] == 0
        assert evidence["details"]["reconcile_lookups"][0]["status"] == "not_found"

    @pytest.mark.unit
    async def test_agent_makes_no_changes_marks_failed(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter returns "ok" but changed nothing
        fake.queue_result(returncode=0)  # git add produces nothing
        fake.queue_result(returncode=0, stdout="")  # diff --cached is empty (no staged)
        fake.queue_result(returncode=0, stdout="0\n")  # rev-list count is 0 — no progress

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "no commits" in (ws.failure_message or "") or "without producing" in (
                ws.failure_message or ""
            )

    @pytest.mark.unit
    async def test_orphan_history_is_recovered_and_pipeline_continues(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Agents sometimes sever git history (e.g. `git checkout --orphan` +
        # fresh commit) — the branch has commits but no shared ancestor
        # with the base. `rev-list --count base..HEAD` can't detect this
        # (count is HIGH — every HEAD commit is "new" when there's no merge
        # base), so the previous no-changes check lets it through, and
        # `gh pr create` dies with GraphQL "no history in common".
        #
        # Recovery: `git reset --soft <base>` keeps the index at the
        # orphan's tree while moving HEAD to base. A fresh commit then
        # squashes the entire orphan chain into one commit on top of base.
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=1, stderr="")  # merge-base is-ancestor: FAIL
        fake.queue_result(returncode=0)  # git reset --soft <base>
        fake.queue_result(returncode=0)  # git commit (re-anchor)
        fake.queue_result(returncode=0)  # merge-base is-ancestor: OK after recovery
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="recovery tests ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/456\n",
        )  # gh pr create

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.subphase == "validation"
            assert ws.last_activity_at is not None
            assert ws.pr_url == "https://github.com/dimileeh/aira-agent/pull/456"
        # reset + commit + verify show up in the call sequence in order.
        reset_call = next(c for c in fake.calls if "reset" in c.args and "--soft" in c.args)
        assert reset_call.args[-1] == "a" * 40  # base_commit
        # Two `merge-base --is-ancestor` calls (pre and post recovery).
        ancestor_calls = [c for c in fake.calls if "merge-base" in c.args]
        assert len(ancestor_calls) == 2

    @pytest.mark.unit
    async def test_orphan_history_fails_loudly_if_recovery_fails(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # If the post-recovery ancestry check still fails (pathological
        # case — e.g. base_commit not reachable), mark failed with a clear
        # message so the operator knows what happened and doesn't chase a
        # ``gh pr create`` GraphQL error.
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=1, stderr="")  # merge-base is-ancestor: FAIL
        fake.queue_result(
            returncode=128, stderr="fatal: unknown revision"
        )  # git reset --soft: FAIL

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "history" in (ws.failure_message or "").lower()
            assert ws.pr_url is None

    @pytest.mark.unit
    async def test_orphan_history_recovery_failure_deposits_planning_artifacts(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An agent that severs git history (orphan branch) where automatic
        # recovery also fails marks the workspace FAILED and returns from inside
        # the post-agent ``try`` BEFORE the post-validation deposit block. The
        # plan + conformance report the agent already wrote into the preserved
        # worktree must still reach the served artifact dir, mirroring the other
        # post-planning failure returns.
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

        async def _agent_run_ok(**_kwargs: Any) -> None:
            # Successful agent/planning run (no failure) so execution reaches
            # the post-agent commit step. The agent already wrote the plan +
            # conformance report into the worktree (seeded above).
            return None

        monkeypatch.setattr(
            executor,
            "_run_agent_task_with_optional_planning",
            _agent_run_ok,
        )

        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # drift: abbrev-ref HEAD
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="")  # diff --cached (already committed)
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=1, stderr="")  # merge-base is-ancestor: FAIL
        fake.queue_result(
            returncode=128, stderr="fatal: unknown revision"
        )  # git reset --soft: FAIL

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "history" in (ws.failure_message or "").lower()

        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").read_text(encoding="utf-8") == (
            '{"status": "satisfied", "gaps": []}'
        )


class TestMonitorHandoff:
    """When a PR monitor is wired, the executor transitions ``pushing →
    monitoring_pr`` and delegates the final transition to the monitor."""

    @pytest.mark.unit
    async def test_hands_off_to_monitor_and_records_pr_number(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        from awf.db.enums import WorkspaceStatus as _WS  # noqa: N814

        class _StubMonitor:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._factory = factory

            async def run(
                self,
                *,
                workspace_id: str,
                compose_project: str,
                compose_file: Path,
            ) -> None:
                self.calls.append(
                    {
                        "workspace_id": workspace_id,
                        "compose_project": compose_project,
                        "compose_file": compose_file,
                    }
                )
                # Pretend the monitor merged + flipped state to completed.
                async with self._factory() as s:
                    ws = await WorkspaceRepository(s).get(workspace_id)
                    assert ws is not None
                    assert ws.status == _WS.monitoring_pr.value
                    await WorkspaceRepository(s).transition(
                        ws, to=_WS.completed, reason_code="STUB_MERGE"
                    )
                    ws.pr_merge_sha = "stub_merge_sha"
                    await s.commit()

        monitor = _StubMonitor()
        compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
        pr = PullRequestCreator(fake)
        ex = WorkspaceExecutor(
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
            pr_monitor=monitor,
        )

        stored_compose_file = tmp_path / "rendered-compose" / "ws" / "compose.yml"
        ws_id = await _seed_ready_workspace(
            factory,
            compose_file_path=str(stored_compose_file),
        )
        # 9-step sequence (same as happy path).
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-web/pull/7777\n",
        )

        await ex.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == _WS.completed.value
            assert ws.pr_url == "https://github.com/dimileeh/aira-web/pull/7777"
            assert ws.pr_number == 7777
            assert ws.remote_push_branch == f"awf/{ws_id}"
            assert ws.pr_merge_sha == "stub_merge_sha"
            transitions = [(e.old_state, e.new_state) for e in ws.events]
            assert ("pushing", "monitoring_pr") in transitions
            assert ("monitoring_pr", "completed") in transitions
        # Monitor received the hand-off call with the right IDs.
        assert len(monitor.calls) == 1
        assert monitor.calls[0]["workspace_id"] == ws_id
        assert monitor.calls[0]["compose_file"] == stored_compose_file


class TestPrNumberExtraction:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://github.com/dimileeh/aira-web/pull/123", 123),
            ("https://github.com/dimileeh/aira-web/pull/123/", 123),
            ("https://github.com/dimileeh/aira-web/pull/123/files", 123),
            ("https://github.com/dimileeh/aira-web/pull/123?notification_referrer_id=abc", 123),
            ("https://github.com/dimileeh/aira-web/pull/123#discussion_r3275054005", 123),
            # Bitbucket PRs use ``/pull-requests/<n>`` — the forge-neutral
            # creation path persists this URL verbatim, so extraction must
            # accept it or the monitor fails with ``missing_pr_number``.
            ("https://bitbucket.org/workspace/repo/pull-requests/7", 7),
            ("https://bitbucket.org/workspace/repo/pull-requests/7/", 7),
            ("https://bitbucket.org/workspace/repo/pull-requests/7/diff", 7),
            ("https://bitbucket.org/workspace/repo/pull-requests/7?foo=bar", 7),
            ("https://bitbucket.org/workspace/repo/pull-requests/7#comment-1", 7),
            ("not a url", None),
            ("https://github.com/dimileeh/aira-web/issues/5", None),
        ],
    )
    def test_extract_pr_number(self, url: str, expected: int | None) -> None:
        from awf.control.executor.helpers import _extract_pr_number

        assert _extract_pr_number(url) == expected


class TestIdempotency:
    @pytest.mark.unit
    async def test_refuses_to_run_on_non_ready_workspace(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Seed then drive to completed via a first execute call.
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0)  # validation
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")  # gh pr create
        await executor.execute(ws_id)

        # Second call must be a no-op — status is completed.
        calls_before = len(fake.calls)
        await executor.execute(ws_id)
        assert len(fake.calls) == calls_before

    @pytest.mark.unit
    async def test_unknown_workspace_is_silent_noop(
        self, executor: WorkspaceExecutor, fake: FakeCommandRunner
    ) -> None:
        await executor.execute("ws_never_existed")
        assert fake.calls == []


class TestPlanningValidationHandoffCleanup:
    @pytest.mark.unit
    async def test_planning_validation_handoff_cleanup_failure_finishes_validate_operation(
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
        await _insert_validate_handoff_recovery_operation(
            factory,
            workspace_id=ws_id,
            operation_id="op_validate_handoff_cleanup_failed",
        )

        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # conformance scope HEAD
        fake.queue_result(
            returncode=124,
            stderr="idle timeout exceeded",
            reason_code=COMMAND_IDLE_TIMEOUT_REASON,
        )
        fake.queue_result(returncode=1, stderr="cleanup still saw tagged processes")

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
                            WHERE id = 'op_validate_handoff_cleanup_failed'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )

        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert EXEC_PROCESS_CLEANUP_FAILED in (ws.failure_message or "")
        assert run == {"status": "succeeded", "reason_code": "VALIDATION_OK"}
        assert operation["status"] == "failed"
        assert operation["error_code"] == EXEC_PROCESS_CLEANUP_FAILED
        assert operation["finished_at"] is not None
        assert EXEC_PROCESS_CLEANUP_FAILED in operation["error_message"]
        result = _json_value(operation["result"])
        assert result["reason_code"] == EXEC_PROCESS_CLEANUP_FAILED
        assert result["validation_run_id"]
