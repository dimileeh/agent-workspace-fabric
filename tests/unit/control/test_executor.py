"""Executor tests with FakeCommandRunner + in-memory SQLite.

Each test drives one workspace through the full pipeline with canned
subprocess output. The single runner handles all compose/adapter/pr calls
since each call is distinguishable by its argv.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    _apply_baseline_coverage_ratchet,
)
from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    ValidationRunner,
)

from .executor_paths import _test_worktrees_root

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


def _queue_pre_push_diagnostics(fake: FakeCommandRunner) -> None:
    """Queue the three canned git results ``PullRequestCreator`` reads
    for its pre-push diagnostic log line (``rev-parse HEAD``,
    ``rev-parse --abbrev-ref HEAD``, ``git log origin/<base>..HEAD``).

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
    fake.queue_result(returncode=0, stdout="deadbeef01\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref
    fake.queue_result(returncode=0, stdout="abc1234 commit\n")  # log ahead-of-base


class TestCoverageBaselineRatchet:
    @pytest.mark.unit
    def test_accepts_below_threshold_coverage_when_baseline_is_preserved(
        self, tmp_path: Path
    ) -> None:
        command = ValidationCommandResult(
            command="pytest --cov=awf --cov-report=term",
            returncode=1,
            duration_seconds=1.0,
            stdout_path=tmp_path / "coverage.stdout",
            stderr_path=tmp_path / "coverage.stderr",
            phase="coverage",
            reason_code="COVERAGE_BELOW_THRESHOLD",
            policy_failed=True,
        )
        result = ValidationResult(
            commands=[command],
            coverage=ValidationCoverageResult(
                provider="python",
                percent=88.25,
                minimum_percent=99.0,
                enforce=True,
                status="failed",
                reason_code="COVERAGE_BELOW_THRESHOLD",
                command_result=command,
            ),
        )
        baseline = ValidationCoverageResult(
            provider="python",
            percent=88.0,
            minimum_percent=99.0,
            enforce=True,
            status="failed",
            reason_code="COVERAGE_BELOW_THRESHOLD",
            command_result=None,
        )

        adjusted = _apply_baseline_coverage_ratchet(result, baseline_coverage=baseline)

        assert adjusted.all_passed
        assert adjusted.coverage is not None
        assert adjusted.coverage.status == "baseline_debt"
        assert adjusted.coverage.reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"
        assert adjusted.commands[0].ok

    @pytest.mark.unit
    def test_keeps_coverage_failed_when_workspace_regresses_baseline(self, tmp_path: Path) -> None:
        command = ValidationCommandResult(
            command="pytest --cov=awf --cov-report=term",
            returncode=1,
            duration_seconds=1.0,
            stdout_path=tmp_path / "coverage.stdout",
            stderr_path=tmp_path / "coverage.stderr",
            phase="coverage",
            reason_code="COVERAGE_BELOW_THRESHOLD",
            policy_failed=True,
        )
        result = ValidationResult(
            commands=[command],
            coverage=ValidationCoverageResult(
                provider="python",
                percent=87.5,
                minimum_percent=99.0,
                enforce=True,
                status="failed",
                reason_code="COVERAGE_BELOW_THRESHOLD",
                command_result=command,
            ),
        )
        baseline = ValidationCoverageResult(
            provider="python",
            percent=88.0,
            minimum_percent=99.0,
            enforce=True,
            status="failed",
            reason_code="COVERAGE_BELOW_THRESHOLD",
            command_result=None,
        )

        adjusted = _apply_baseline_coverage_ratchet(result, baseline_coverage=baseline)

        assert not adjusted.all_passed
        assert adjusted.coverage is not None
        assert adjusted.coverage.reason_code == "COVERAGE_BELOW_THRESHOLD"


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

class TestHappyPath:
    @pytest.mark.unit
    async def test_claim_ready_persists_execution_claim(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        ws = await executor._claim_ready(
            ws_id,
            execution_owner_id="worker-a",
            execution_lease_expires_at=lease_expires_at,
        )

        assert ws is not None
        async with factory() as s:
            persisted = await WorkspaceRepository(s).get(ws_id)
            assert persisted is not None
            assert persisted.status == WorkspaceStatus.running.value
            assert persisted.execution_claimed_by == "worker-a"
            assert persisted.execution_claim_expires_at == lease_expires_at.replace(tzinfo=None)

    @pytest.mark.unit
    async def test_drives_ready_to_completed_and_records_pr_url(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)

        # Queue results for the full sequence:
        # (1) adapter.run, (2) branch-drift check, (3) git add -A,
        # (4) git diff --cached --name-only, (5) git commit,
        # (6) git rev-list --count base..HEAD,
        # (7) git merge-base --is-ancestor base HEAD,
        # (8) validation (one test cmd), (9) git push, (10) gh pr create.
        fake.queue_result(returncode=0, stdout="codex finished")  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff (non-empty)
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/123\n",
        )  # gh pr create

        await executor.execute(ws_id)

        commit_calls = [call.args for call in fake.calls if "commit" in call.args]
        assert commit_calls
        assert any(arg.startswith("safe.directory=") for arg in commit_calls[0])
        assert "user.name=AWF Agent" in commit_calls[0]
        assert "user.email=awf@example.com" in commit_calls[0]

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/dimileeh/aira-agent/pull/123"

    @pytest.mark.unit
    async def test_task_policy_agent_model_overrides_adapter_default(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(
            factory,
            agent="opencode",
            task_policy={"agent_model": "ollama/gemma4:31b-cloud"},
        )

        fake.queue_result(returncode=0, stdout="opencode finished")  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/124\n",
        )  # gh pr create

        await executor.execute(ws_id)

        adapter_args = fake.calls[0].args
        assert "--model" in adapter_args
        assert "ollama/gemma4:31b-cloud" in adapter_args

    @pytest.mark.unit
    async def test_planning_profile_runs_plan_execute_compare_before_validation(
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
                    "plan_path": "docs/awf-plans/{workspace_id}.md",
                    "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
                    "max_iterations": 1,
                    "enforce_plan_only_changes": True,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )

        fake.queue_result(returncode=0, stdout="")  # changed paths before planning
        fake.queue_result(returncode=0, stdout="plan written")  # planning adapter
        fake.queue_result(  # changed paths after planning
            returncode=0,
            stdout=f"?? docs/awf-plans/{ws_id}.md\n",
        )
        fake.queue_result(returncode=0, stdout="implemented")  # execution adapter
        fake.queue_result(  # changed paths before compare
            returncode=0,
            stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/awf/foo.py\n",
        )
        fake.queue_result(  # conformance adapter
            returncode=0,
            stdout='{"status":"satisfied","summary":"plan achieved","gaps":[]}',
        )
        fake.queue_result(  # changed paths after compare
            returncode=0,
            stdout=(
                f"?? docs/awf-plans/{ws_id}.md\n"
                f"?? docs/awf-plans/{ws_id}.conformance.json\n"
                " M src/awf/foo.py\n"
            ),
        )
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        adapter_prompts = [
            call.args[-1]
            for call in fake.calls
            if call.args[:2] == ["docker", "compose"] and call.args[-1].startswith("## ")
        ]
        assert len(adapter_prompts) == 3
        assert "Planning phase" in adapter_prompts[0]
        assert "Execution phase" in adapter_prompts[1]
        assert "Conformance phase" in adapter_prompts[2]

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value

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
        fake.queue_result(returncode=0, stdout="plan written")  # planning
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="implemented")  # initial execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(  # compare says not done
            returncode=0,
            stdout='{"status":"needs_iteration","summary":"gap","gaps":["add tests"]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="fixed gap")  # iteration execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(  # compare satisfied
            returncode=0,
            stdout='{"status":"satisfied","summary":"done","gaps":[]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="src/x.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="tests ok")
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        adapter_prompts = [
            call.args[-1]
            for call in fake.calls
            if call.args[:2] == ["docker", "compose"] and call.args[-1].startswith("## ")
        ]
        assert len(adapter_prompts) == 5
        assert "Iteration 1" in adapter_prompts[3]

    @pytest.mark.unit
    async def test_planning_profile_fails_when_plan_phase_changes_code(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {"required": True, "enforce_plan_only_changes": True},
            },
        )

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="plan plus code")  # planning
        fake.queue_result(  # after planning
            returncode=0,
            stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/awf/oops.py\n",
        )

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "planning phase changed files outside" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_records_all_expected_transitions(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        # Same 8-step sequence as the happy-path test.
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0)  # validation
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            transitions = [(e.old_state, e.new_state) for e in ws.events]
            assert ("ready", "running") in transitions
            assert ("running", "validating") in transitions
            assert ("validating", "pushing") in transitions
            assert ("pushing", "completed") in transitions

    @pytest.mark.unit
    async def test_records_tier1_validation_run_provenance(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(
            factory,
            test_commands=["ruff check .", "pytest -q"],
        )
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0, stdout="ruff ok")  # validation cmd 1
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd 2
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT
                            workspace_id,
                            attempt_id,
                            tier,
                            command_set_hash,
                            commands,
                            base_commit,
                            target_branch,
                            target_head_sha,
                            status,
                            reason_code,
                            started_at,
                            finished_at,
                            log_stream_refs
                        FROM validation_runs
                        WHERE workspace_id = :workspace_id
                        """
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .all()
            )

        assert len(rows) == 1
        run = rows[0]
        assert run["workspace_id"] == ws_id
        assert run["attempt_id"] is None
        assert run["tier"] == 1
        assert isinstance(run["command_set_hash"], str)
        assert len(run["command_set_hash"]) == 64
        assert json.loads(run["commands"]) == [
            {
                "phase": "validate",
                "command_index": 1,
                "command": "ruff check .",
                "stream_ids": {
                    "stdout": "validation.01_validate.stdout",
                    "stderr": "validation.01_validate.stderr",
                },
                "retry_count": 0,
            },
            {
                "phase": "validate",
                "command_index": 2,
                "command": "pytest -q",
                "stream_ids": {
                    "stdout": "validation.02_validate.stdout",
                    "stderr": "validation.02_validate.stderr",
                },
                "retry_count": 0,
            },
        ]
        assert run["base_commit"] == "a" * 40
        assert run["target_branch"] == f"awf/{ws_id}"
        assert run["target_head_sha"] == "deadbeef01"
        assert run["status"] == "succeeded"
        assert run["reason_code"] == "VALIDATION_OK"
        assert run["started_at"] is not None
        assert run["finished_at"] is not None
        assert json.loads(run["log_stream_refs"]) == {
            "commands": [
                {
                    "stdout": "validation.01_validate.stdout",
                    "stderr": "validation.01_validate.stderr",
                },
                {
                    "stdout": "validation.02_validate.stdout",
                    "stderr": "validation.02_validate.stderr",
                },
            ]
        }

    @pytest.mark.unit
    async def test_recovery_validation_records_required_tier_and_finishes_operation(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory, test_commands=["ruff check ."])
        async with factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE workspaces
                    SET task_class = 'refactor_task'
                    WHERE id = :workspace_id
                    """
                ),
                {"workspace_id": ws_id},
            )
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
                        'op_validate_recovery',
                        :workspace_id,
                        'validate',
                        'pending',
                        '{"reason":"validation_insufficient_tier"}',
                        :created_at
                    )
                    """
                ),
                {"workspace_id": ws_id, "created_at": datetime.now(UTC)},
            )
            await session.commit()

        fake.queue_result(returncode=0, stdout="codex finished")  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0, stdout="ruff ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1\n")

        await executor.execute(ws_id)

        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT tier, status
                        FROM validation_runs
                        WHERE workspace_id = :workspace_id
                        """
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .all()
            )
            operations = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT status, payload, result, finished_at
                        FROM operations
                        WHERE id = 'op_validate_recovery'
                        """
                        )
                    )
                )
                .mappings()
                .one()
            )

        assert rows == [{"tier": 2, "status": "succeeded"}]
        assert operations["status"] == "succeeded"
        assert operations["finished_at"] is not None
        assert json.loads(operations["payload"])["requested_tier"] == 2
        assert json.loads(operations["result"])["requested_tier"] == 2


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
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(
            returncode=0, stdout="tests/e2e/bff/tasks.spec.ts\n"
        )  # cached diff: real work
        fake.queue_result(returncode=0)  # git commit (AWF's auto-commit)
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
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
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached (non-empty)
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
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
                    "coverage": {
                        "minimum_percent": 99,
                        "enforce": True,
                        "command": "pytest --cov=awf --cov-report=term",
                    }
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
        assert json.loads(run["log_stream_refs"])["coverage"] == {
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
        assert json.loads(operation["result"])["coverage"] == {
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
    async def test_push_failure_marks_failed_with_infrastructure_reason(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter ok
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0)  # validation ok
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=128, stderr="remote: perm denied")  # push fails

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"

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
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=1, stderr="")  # merge-base is-ancestor: FAIL
        fake.queue_result(returncode=0)  # git reset --soft <base>
        fake.queue_result(returncode=0)  # git commit (re-anchor)
        fake.queue_result(returncode=0)  # merge-base is-ancestor: OK after recovery
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
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
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
            ("not a url", None),
            ("https://github.com/dimileeh/aira-web/issues/5", None),
        ],
    )
    def test_extract_pr_number(self, url: str, expected: int | None) -> None:
        from awf.control.executor import _extract_pr_number

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
