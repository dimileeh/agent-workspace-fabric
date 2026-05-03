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
from awf.adapters.base import AgentAdapter
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    _apply_baseline_coverage_ratchet,
)
from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
)
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


def _queue_pre_push_diagnostics(fake: FakeCommandRunner, *, head: str = "deadbeef01") -> None:
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
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # rev-parse HEAD
    fake.queue_result(returncode=0, stdout="awf/ws_test\n")  # abbrev-ref
    fake.queue_result(returncode=0, stdout="abc1234 commit\n")  # log ahead-of-base


def _queue_validation_head(fake: FakeCommandRunner, head: str = "deadbeef01") -> None:
    fake.queue_result(returncode=0, stdout=f"{head}\n")  # pre-validation rev-parse HEAD


def _created_pr_body(fake: FakeCommandRunner) -> str:
    create_call = next(call.args for call in fake.calls if call.args[:3] == ["gh", "pr", "create"])
    return create_call[create_call.index("--body") + 1]


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
        _queue_validation_head(fake)
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
            assert push_events[0].reason_code == "PR_OPENED"
            assert push_events[0].payload == {
                "schema": "control_audit.v1",
                "actor": "executor",
                "source": "executor",
                "action": "git_push",
                "outcome": "succeeded",
                "reason_code": "PR_OPENED",
                "pr_number": 123,
                "pr_url": "https://github.com/dimileeh/aira-agent/pull/123",
                "source_head_sha": "deadbeef01",
                "source_base_sha": "a" * 40,
                "target_branch": "development",
                "remote_branch": f"awf/{ws_id}",
                "branch_name": f"awf/{ws_id}",
            }
            assert len(pr_events) == 1
            assert pr_events[0].reason_code == "PR_OPENED"
            assert pr_events[0].payload == {
                "schema": "control_audit.v1",
                "actor": "executor",
                "source": "executor",
                "action": "pr_create",
                "outcome": "succeeded",
                "reason_code": "PR_OPENED",
                "pr_number": 123,
                "pr_url": "https://github.com/dimileeh/aira-agent/pull/123",
                "source_head_sha": "deadbeef01",
                "source_base_sha": "a" * 40,
                "target_branch": "development",
                "remote_branch": f"awf/{ws_id}",
                "branch_name": f"awf/{ws_id}",
            }
        pr_body = _created_pr_body(fake)
        assert f"Automatically opened by AWF workspace `{ws_id}`" in pr_body
        assert "(agent: `codex`, model: `gpt-5`, effort: `xhigh`)." in pr_body

    @pytest.mark.unit
    async def test_reuses_existing_pr_audit_event(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.pr_url = "https://github.com/dimileeh/aira-agent/pull/321"
            ws.pr_number = 321
            await s.commit()

        fake.queue_result(returncode=0, stdout="codex finished")
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")
        _queue_pre_push_diagnostics(fake, head="reuse-head")
        fake.queue_result(returncode=0)

        await executor.execute(ws_id)

        assert not any(call.args[:3] == ["gh", "pr", "create"] for call in fake.calls)
        async with factory() as s:
            pr_events = await WorkspaceEventRepository(s).list(
                workspace_id=ws_id,
                event_type="workspace.audit.pr_created",
                limit=10,
            )
        assert len(pr_events) == 1
        assert pr_events[0].payload is not None
        assert pr_events[0].payload["outcome"] == "reused"
        assert pr_events[0].payload["reason_code"] == "PR_UPDATED"
        assert pr_events[0].payload["pr_number"] == 321
        assert pr_events[0].payload["pr_url"] == "https://github.com/dimileeh/aira-agent/pull/321"

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
        _queue_validation_head(fake)
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
        pr_body = _created_pr_body(fake)
        assert "(agent: `opencode`, model: `ollama/gemma4:31b-cloud`, effort: `xhigh`)." in pr_body

    @pytest.mark.unit
    async def test_pr_monitor_receives_adapter_bound_to_workspace_model(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        captured: list[tuple[str | None, str | None]] = []

        class Monitor:
            async def run(self, **_: object) -> None:
                return None

        def monitor_factory(adapter: AgentAdapter, *_: object) -> Monitor:
            captured.append(
                (
                    adapter._default_model,
                    adapter._default_effort,
                )
            )
            return Monitor()

        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE),
            validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
            pr_creator=PullRequestCreator(fake),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
            ),
            pr_monitor_factory=monitor_factory,
        )
        ws_id = await _seed_ready_workspace(
            factory,
            agent="opencode",
            task_policy={"agent_model": "ollama/glm-5.1:cloud"},
        )

        fake.queue_result(returncode=0, stdout="opencode finished")  # adapter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/125\n",
        )  # gh pr create

        await executor.execute(ws_id)

        assert captured == [("ollama/glm-5.1:cloud", "xhigh")]

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
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning adapter
        fake.queue_result(  # changed paths after planning
            returncode=0,
            stdout=f"?? docs/awf-plans/{ws_id}.md\n",
        )
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # rev-parse HEAD pre-loop
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
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # rev-parse HEAD post-iter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
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

        adapter_prompts = [
            call.args[-1]
            for call in fake.calls
            if call.args[:2] == ["docker", "compose"] and call.args[-1].startswith("## ")
        ]
        assert len(adapter_prompts) == 5
        assert "Iteration 1" in adapter_prompts[3]

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

    @pytest.mark.unit
    async def test_planning_profile_records_conformance_stall_when_compare_idle_timeout_after_implementation_commits(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typing import Any

        from awf.adapters import base as adapter_base
        from awf.adapters.base import AgentRunResult
        from awf.common.commands import CommandResult
        from awf.control import executor as executor_module
        from awf.db.enums import AgentRuntime
        from awf.runtime.planning import AGENT_STALLED_IN_CONFORMANCE

        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "max_iterations": 1,
                    "conformance_stall": {
                        "no_output_seconds": 600,
                        "over_duration_seconds": 1800,
                        "repeated_output_threshold": 3,
                        "recovery_action": "proceed_to_validation",
                    },
                },
            },
        )

        # Drive iteration_started_at -> elapsed_seconds past
        # no_output_seconds=600 so the policy threshold is met and the idle
        # timeout is recorded as AGENT_STALLED_IN_CONFORMANCE.
        clock = [0.0]

        def _fake_monotonic() -> float:
            clock[0] += 700.0
            return clock[0]

        monkeypatch.setattr(executor_module.time, "monotonic", _fake_monotonic)

        class _IdleConformanceAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.codex

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            def get_provider(self, model: str | None) -> str:
                return "openai"

            def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
                return []

            async def run(self, *, prompt: str, **kwargs: Any) -> AgentRunResult:
                if "## Conformance phase" in prompt:
                    raise adapter_base.AgentRunError(
                        agent=self.name,
                        result=CommandResult(
                            returncode=124,
                            stdout="",
                            stderr="idle timeout exceeded after 600s",
                        ),
                        reason_code="AGENT_IDLE_TIMEOUT",
                    )
                return AgentRunResult(returncode=0, stdout="ok", stderr="")

        monkeypatch.setitem(
            adapter_base._REGISTRY, AgentRuntime.codex, _IdleConformanceAdapter
        )

        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE),
            validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
            pr_creator=PullRequestCreator(fake),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
            ),
        )

        # The agent commits the plan artifact during planning (allowed by the
        # scope check) so pre- and post-planning HEADs differ. The stall
        # commit metrics must use the post-planning HEAD so the planning
        # commit is excluded from ``implementation_commit_count``.
        fake.queue_result(returncode=0, stdout="")  # before planning git status
        fake.queue_result(returncode=0, stdout="sha_pre\n")  # rev-parse HEAD baseline
        # Planning adapter (custom) — no runner call
        fake.queue_result(returncode=0, stdout="")  # changed_paths after planning (plan committed, not dirty)
        fake.queue_result(  # committed_paths_since (planning committed the plan)
            returncode=0,
            stdout=f"docs/awf-plans/{ws_id}.md\n",
        )
        fake.queue_result(returncode=0, stdout="sha_post\n")  # rev-parse HEAD pre-loop
        # Iteration 0:
        # Execute adapter (custom) — no runner call
        fake.queue_result(  # before_compare git status
            returncode=0,
            stdout=" M src/awf/foo.py\n",
        )
        # Conformance adapter raises AgentRunError; executor still recomputes
        # after_compare so the fail_on_unexplained_deviation scope check
        # applies on the timeout branch (no extra paths here), then captures
        # HEAD for the iteration-end progress digest.
        fake.queue_result(  # after_compare git status (post-timeout)
            returncode=0,
            stdout=" M src/awf/foo.py\n",
        )
        fake.queue_result(returncode=0, stdout="sha_post\n")  # rev-parse HEAD iter 0 post
        # After raise, executor introspects implementation commits for stall evidence
        fake.queue_result(returncode=0, stdout="head_sha_after\n")  # post-stall rev-parse HEAD
        fake.queue_result(returncode=0, stdout="2\n")  # post-stall rev-list count
        fake.queue_result(  # post-stall git diff --name-only base..HEAD
            returncode=0,
            stdout="src/awf/foo.py\nsrc/awf/bar.py\n",
        )

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
            assert failed_event.reason_code == AGENT_STALLED_IN_CONFORMANCE
            assert failed_event.payload is not None
            stall = failed_event.payload["details"]["conformance_stall"]
            assert stall["kind"] == "no_output"
            assert stall["reason_code"] == AGENT_STALLED_IN_CONFORMANCE
            assert stall["plan_path"] == f"docs/awf-plans/{ws_id}.md"
            assert stall["report_path"] == f"docs/awf-plans/{ws_id}.conformance.json"
            assert stall["salvage_hint"]["implementation_commit_count"] == 2
            assert stall["salvage_hint"]["base_sha"] == "sha_post"
            assert stall["recovery_action"] == "proceed_to_validation"
            assert failed_event.payload["salvage"]["worktree_path"]
            stall_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.planning_conformance_stalled"
            ]
            assert len(stall_events) == 1
            assert stall_events[0].reason_code == AGENT_STALLED_IN_CONFORMANCE
            assert stall_events[0].payload is not None
            assert stall_events[0].payload["kind"] == "no_output"
            assert stall_events[0].payload["recovery_action"] == "proceed_to_validation"

        # The stall-failure rev-list/diff calls must scope from the
        # post-planning HEAD, not the pre-planning baseline; otherwise the
        # plan-artifact commit made during planning would inflate
        # ``implementation_commit_count``.
        revlist_calls = [
            call for call in fake.calls
            if "rev-list" in call.args and "--count" in call.args
        ]
        assert len(revlist_calls) == 1
        assert "sha_post..HEAD" in revlist_calls[0].args
        post_stall_diff = [
            call for call in fake.calls
            if "diff" in call.args
            and "--name-only" in call.args
            and "sha_post..HEAD" in call.args
        ]
        assert len(post_stall_diff) == 1

    @pytest.mark.unit
    async def test_planning_profile_ignores_stale_satisfied_report_on_compare_idle_timeout(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A satisfied JSON sitting at ``report_path`` before the conformance
        call (e.g., left by a prior interrupted AWF run on the same workspace)
        must not short-circuit the loop on AGENT_IDLE_TIMEOUT. The timeout
        branch is required to honor only a report whose digest changed during
        the current compare call; otherwise the iteration is treated as
        no_output by the stall classifier.
        """
        from typing import Any

        from awf.adapters import base as adapter_base
        from awf.adapters.base import AgentRunResult
        from awf.common.commands import CommandResult
        from awf.control import executor as executor_module
        from awf.db.enums import AgentRuntime
        from awf.runtime.planning import AGENT_STALLED_IN_CONFORMANCE

        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "max_iterations": 1,
                    "conformance_stall": {
                        "no_output_seconds": 600,
                        "over_duration_seconds": 1800,
                        "repeated_output_threshold": 3,
                        "recovery_action": "proceed_to_validation",
                    },
                },
            },
        )

        # Plant a stale satisfied report at the configured path BEFORE the
        # executor runs. The conformance call will idle out without writing
        # anything, so without the freshness guard the success short-circuit
        # would falsely fire on this leftover JSON.
        worktree_path = _test_worktrees_root(factory) / ws_id
        report_dir = worktree_path / "docs" / "awf-plans"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{ws_id}.conformance.json").write_text(
            '{"status":"satisfied","summary":"stale leftover","gaps":[]}',
            encoding="utf-8",
        )

        clock = [0.0]

        def _fake_monotonic() -> float:
            clock[0] += 700.0
            return clock[0]

        monkeypatch.setattr(executor_module.time, "monotonic", _fake_monotonic)

        class _IdleConformanceAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.codex

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            def get_provider(self, model: str | None) -> str:
                return "openai"

            def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
                return []

            async def run(self, *, prompt: str, **kwargs: Any) -> AgentRunResult:
                if "## Conformance phase" in prompt:
                    raise adapter_base.AgentRunError(
                        agent=self.name,
                        result=CommandResult(
                            returncode=124,
                            stdout="",
                            stderr="idle timeout exceeded after 600s",
                        ),
                        reason_code="AGENT_IDLE_TIMEOUT",
                    )
                return AgentRunResult(returncode=0, stdout="ok", stderr="")

        monkeypatch.setitem(
            adapter_base._REGISTRY, AgentRuntime.codex, _IdleConformanceAdapter
        )

        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE),
            validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
            pr_creator=PullRequestCreator(fake),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
            ),
        )

        # The stale satisfied JSON pre-exists planning, so git sees it as
        # untracked from the start. The planning phase then adds the plan
        # artifact alongside it. The conformance JSON stays unchanged across
        # both phases, so it never registers as a phase-introduced path and
        # the planning scope check does not fire on it.
        stale_only_status = f"?? docs/awf-plans/{ws_id}.conformance.json\n"
        plan_plus_stale_status = (
            f"?? docs/awf-plans/{ws_id}.md\n"
            f"?? docs/awf-plans/{ws_id}.conformance.json\n"
        )

        fake.queue_result(  # before planning git status (stale JSON already present)
            returncode=0,
            stdout=stale_only_status,
        )
        fake.queue_result(returncode=0, stdout="sha_pre\n")  # rev-parse HEAD baseline
        # planning adapter (custom) — no runner call
        fake.queue_result(  # changed_paths after planning (plan added; stale persists)
            returncode=0,
            stdout=plan_plus_stale_status,
        )
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
        fake.queue_result(returncode=0, stdout="sha_pre\n")  # rev-parse HEAD pre-loop
        # iteration 0:
        # execute adapter (custom) — no runner call
        fake.queue_result(  # before_compare git status
            returncode=0,
            stdout=plan_plus_stale_status,
        )
        # conformance adapter raises AgentRunError
        fake.queue_result(  # after_compare git status (post-timeout, unchanged)
            returncode=0,
            stdout=plan_plus_stale_status,
        )
        fake.queue_result(returncode=0, stdout="sha_pre\n")  # rev-parse HEAD iter 0 post
        # post-stall introspection
        fake.queue_result(returncode=0, stdout="head_sha_after\n")  # post-stall rev-parse HEAD
        fake.queue_result(returncode=0, stdout="0\n")  # post-stall rev-list count
        fake.queue_result(returncode=0, stdout="")  # post-stall git diff

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            # The stale satisfied JSON must not flip the workspace to a
            # successful completion. Expect the no_output stall instead.
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            failed_event = next(
                event
                for event in reversed(ws.events)
                if event.event_type == "workspace.state_changed"
                and event.new_state == WorkspaceStatus.failed.value
            )
            assert failed_event.reason_code == AGENT_STALLED_IN_CONFORMANCE
            assert failed_event.payload is not None
            stall = failed_event.payload["details"]["conformance_stall"]
            assert stall["kind"] == "no_output"
            assert stall["reason_code"] == AGENT_STALLED_IN_CONFORMANCE
            # last_report_digest must not match the stale on-disk JSON; the
            # iteration is treated as if no report was produced.
            assert stall.get("last_report_digest") is None

    @pytest.mark.unit
    async def test_planning_profile_does_not_record_stall_for_deterministic_needs_iteration_within_budget(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from awf.runtime.planning import AGENT_STALLED_IN_CONFORMANCE

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

        # Same queue as test_planning_profile_iterates_when_conformance_reports_gaps
        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # initial execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(  # compare says not done (different summary each time)
            returncode=0,
            stdout='{"status":"needs_iteration","summary":"gap-1","gaps":["add tests"]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
        fake.queue_result(returncode=0, stdout="fixed gap")  # iteration execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n M src/y.py\n")
        fake.queue_result(  # compare satisfied
            returncode=0,
            stdout='{"status":"satisfied","summary":"done","gaps":[]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n M src/y.py\n")
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

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            failed_events = [
                event
                for event in ws.events
                if event.reason_code == AGENT_STALLED_IN_CONFORMANCE
            ]
            assert failed_events == []
            stall_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.planning_conformance_stalled"
            ]
            assert stall_events == []

    @pytest.mark.unit
    async def test_planning_profile_continues_after_slow_productive_needs_iteration(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.control import executor as executor_module
        from awf.runtime.planning import AGENT_STALLED_IN_CONFORMANCE

        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "max_iterations": 1,
                    "conformance_stall": {
                        "no_output_seconds": 600,
                        "over_duration_seconds": 10,
                        "repeated_output_threshold": 3,
                    },
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )

        clock = [0.0]

        def _fake_monotonic() -> float:
            clock[0] += 30.0
            return clock[0]

        monkeypatch.setattr(executor_module.time, "monotonic", _fake_monotonic)

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
            stdout='{"status":"needs_iteration","summary":"gap-1","gaps":["add tests"]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
        fake.queue_result(returncode=0, stdout="fixed gap")  # iteration execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/y.py\n")
        fake.queue_result(
            returncode=0,
            stdout='{"status":"satisfied","summary":"done","gaps":[]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/y.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 1 post
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="src/x.py\nsrc/y.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert [
                event
                for event in ws.events
                if event.reason_code == AGENT_STALLED_IN_CONFORMANCE
            ] == []

    @pytest.mark.unit
    async def test_planning_profile_records_stall_when_report_digest_repeats_without_progress(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        from awf.runtime.planning import AGENT_STALLED_IN_CONFORMANCE

        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "max_iterations": 5,
                    "conformance_stall": {
                        "no_output_seconds": 600,
                        "over_duration_seconds": 1800,
                        "repeated_output_threshold": 3,
                    },
                },
            },
        )

        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE),
            validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
            pr_creator=PullRequestCreator(fake),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
            ),
        )

        identical_report = (
            '{"status":"needs_iteration","summary":"same gap","gaps":["finish tests"]}'
        )
        identical_paths = f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n"

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning adapter
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (planning clean)
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop

        # Iteration 0 introduces src/x.py (worktree_changed=True), then three
        # follow-up iterations leave the worktree untouched (worktree_changed=False).
        # The repeated_output stall fires once the no-progress streak hits the
        # threshold (3) at the end of iteration 3. HEAD stays at sha1 across
        # iterations so the progress digest only flips on dirty-content changes.
        for _ in range(4):
            fake.queue_result(returncode=0, stdout="execute output")  # execute adapter
            fake.queue_result(returncode=0, stdout=identical_paths)  # before_compare
            fake.queue_result(returncode=0, stdout=identical_report)  # conformance adapter
            fake.queue_result(returncode=0, stdout=identical_paths)  # after_compare
            fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter post

        # post-stall git introspection
        fake.queue_result(returncode=0, stdout="head_sha_after\n")  # rev-parse HEAD
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0, stdout="src/x.py\n")  # diff --name-only

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
            assert failed_event.reason_code == AGENT_STALLED_IN_CONFORMANCE
            assert failed_event.payload is not None
            stall = failed_event.payload["details"]["conformance_stall"]
            assert stall["kind"] == "repeated_output"
            assert stall["repeated_output_count"] == 3

    @pytest.mark.unit
    async def test_planning_profile_does_not_record_stall_when_iterations_commit_each_round(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Repeated identical conformance reports must not trip the stall when
        the agent is committing implementation work each iteration.

        Without folding HEAD into the progress digest, an agent that commits
        leaves a clean working tree (empty dirty path set) and the stall
        detector sees worktree_changed=False every iteration even though
        real implementation progress is happening. This test pins down the
        commit-progression path: HEAD advances per iteration, and the loop
        eventually reaches satisfied without falsely raising a stall.
        """
        from awf.runtime.planning import AGENT_STALLED_IN_CONFORMANCE

        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "max_iterations": 5,
                    "conformance_stall": {
                        "no_output_seconds": 600,
                        "over_duration_seconds": 1800,
                        "repeated_output_threshold": 3,
                    },
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )

        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=fake,
            compose=ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE),
            validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
            pr_creator=PullRequestCreator(fake),
            config=ExecutorConfig(
                worktrees_root=tmp_path / "work" / "worktrees",
                compose_projects_root=tmp_path / "work" / "compose",
            ),
        )

        identical_report = (
            '{"status":"needs_iteration","summary":"same gap","gaps":["finish tests"]}'
        )
        satisfied_report = '{"status":"satisfied","summary":"done","gaps":[]}'
        # Working tree stays clean every iteration because the agent commits
        # its work; only HEAD moves. This is the scenario the original digest
        # missed.
        clean_paths = ""

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha0\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning adapter
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(  # committed_paths_since (planning committed plan)
            returncode=0,
            stdout=f"docs/awf-plans/{ws_id}.md\n",
        )
        fake.queue_result(returncode=0, stdout="sha_plan\n")  # rev-parse HEAD pre-loop

        # Three iterations with identical clean working tree and identical
        # report digest, but each iteration the agent commits → HEAD moves.
        # The repeated_output threshold is 3, so without HEAD in the digest
        # this would falsely fire.
        for sha in ("sha_iter0", "sha_iter1", "sha_iter2"):
            fake.queue_result(returncode=0, stdout="execute output")  # execute
            fake.queue_result(returncode=0, stdout=clean_paths)  # before_compare
            fake.queue_result(returncode=0, stdout=identical_report)  # conformance
            fake.queue_result(returncode=0, stdout=clean_paths)  # after_compare
            fake.queue_result(returncode=0, stdout=f"{sha}\n")  # rev-parse HEAD iter post

        # Fourth iteration: agent finally satisfies the plan (commits + report flips)
        fake.queue_result(returncode=0, stdout="execute output")  # execute
        fake.queue_result(returncode=0, stdout=clean_paths)  # before_compare
        fake.queue_result(returncode=0, stdout=satisfied_report)  # conformance
        fake.queue_result(returncode=0, stdout=clean_paths)  # after_compare
        fake.queue_result(returncode=0, stdout="sha_iter3\n")  # rev-parse HEAD iter post

        # Post-loop validation/PR queue — the workspace should reach completion.
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="src/x.py\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            stall_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.planning_conformance_stalled"
                or event.reason_code == AGENT_STALLED_IN_CONFORMANCE
            ]
            assert stall_events == []

    @pytest.mark.unit
    async def test_planning_profile_does_not_record_stall_when_satisfied_iteration_exceeds_over_duration(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.control import executor as executor_module
        from awf.runtime.planning import AGENT_STALLED_IN_CONFORMANCE

        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "max_iterations": 1,
                    "conformance_stall": {
                        "no_output_seconds": 600,
                        "over_duration_seconds": 10,
                        "repeated_output_threshold": 3,
                    },
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )

        # Drive iteration_started_at -> elapsed_seconds beyond
        # over_duration_seconds=10 so the cumulative-time stall would fire if
        # the success short-circuit were missing.
        clock = [0.0]

        def _fake_monotonic() -> float:
            clock[0] += 30.0
            return clock[0]

        monkeypatch.setattr(executor_module.time, "monotonic", _fake_monotonic)

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="base_sha\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning adapter
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
        fake.queue_result(returncode=0, stdout="base_sha\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # execute adapter
        fake.queue_result(  # before_compare
            returncode=0,
            stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/awf/foo.py\n",
        )
        fake.queue_result(  # conformance returns satisfied despite slow run
            returncode=0,
            stdout='{"status":"satisfied","summary":"plan achieved","gaps":[]}',
        )
        fake.queue_result(  # after_compare
            returncode=0,
            stdout=(
                f"?? docs/awf-plans/{ws_id}.md\n"
                f"?? docs/awf-plans/{ws_id}.conformance.json\n"
                " M src/awf/foo.py\n"
            ),
        )
        fake.queue_result(returncode=0, stdout="base_sha\n")  # rev-parse HEAD iter 0 post
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            stall_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.planning_conformance_stalled"
                or event.reason_code == AGENT_STALLED_IN_CONFORMANCE
            ]
            assert stall_events == []

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
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        _queue_validation_head(fake)
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
        _queue_validation_head(fake, head="validation-workspace-head")
        fake.queue_result(returncode=0, stdout="ruff ok")  # validation cmd 1
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd 2
        _queue_pre_push_diagnostics(fake, head="pr-target-head")
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
                            base_sha,
                            workspace_head_sha,
                            target_branch,
                            target_head_sha,
                            profile_name,
                            profile_version,
                            profile_source,
                            resolved_profile_digest,
                            environment_identity_digest,
                            environment_identity_inputs,
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
        assert run["base_sha"] == "a" * 40
        assert run["workspace_head_sha"] == "validation-workspace-head"
        assert run["target_branch"] == f"awf/{ws_id}"
        assert run["target_head_sha"] == "pr-target-head"
        assert isinstance(run["profile_name"], str)
        assert run["profile_name"]
        assert isinstance(run["profile_version"], int)
        assert isinstance(run["profile_source"], str)
        assert len(run["resolved_profile_digest"]) == 64
        assert len(run["environment_identity_digest"]) == 64
        identity_inputs = json.loads(run["environment_identity_inputs"])
        assert identity_inputs["schema_version"] == 1
        assert "runtime" in identity_inputs
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
        _queue_validation_head(fake)
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
        commands = json.loads(run["commands"])
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
                "remote: perm denied for "
                "https://user:ghp_should_not_persist@github.com/org/repo"
            ),
        )  # push fails

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert "ghp_should_not_persist" not in (ws.failure_message or "")
            assert "https://[redacted]@github.com/org/repo" in (
                ws.failure_message or ""
            )
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
                "error_message": (
                    "remote: perm denied for "
                    "https://[redacted]@github.com/org/repo"
                ),
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
            assert "https://[redacted]@github.com/org/repo" in (
                ws.failure_message or ""
            )
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
