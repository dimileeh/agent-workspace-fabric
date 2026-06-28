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
from awf.adapters.base import AgentAdapter, AgentRunResult
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.profiles.models import WorkspaceProfile
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


class TestHappyPathPart003:
    @pytest.mark.unit
    async def test_planning_accepts_existing_plan_created_without_git_status_delta(
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
                    "max_iterations": 0,
                    "fail_on_unexplained_deviation": False,
                },
            },
        )
        worktree_path = _test_worktrees_root(factory) / ws_id
        plan_path = Path("docs") / "awf-plans" / f"{ws_id}.md"

        class _PlanWritingAdapter(AgentAdapter):
            runtime = AgentRuntime.codex

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            def get_provider(self, model: str | None) -> str:
                return "openai"

            def _cli_args(self, *, model: str | None) -> list[str]:
                return []

            async def run(self, *, prompt: str, **kwargs: Any) -> AgentRunResult:
                if "## Planning phase" in prompt:
                    full_plan_path = worktree_path / plan_path
                    full_plan_path.parent.mkdir(parents=True, exist_ok=True)
                    full_plan_path.write_text(
                        "# Plan\n\n- implement the change\n", encoding="utf-8"
                    )
                    return AgentRunResult(returncode=0, stdout="plan written", stderr="")
                if "## Conformance phase" in prompt:
                    return AgentRunResult(
                        returncode=0,
                        stdout='{"status":"satisfied","summary":"done","gaps":[]}',
                        stderr="",
                    )
                return AgentRunResult(returncode=0, stdout="implemented", stderr="")

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None

        profile = WorkspaceProfile.model_validate(
            {
                "name": "planned",
                "planning": {
                    "required": True,
                    "plan_path": "docs/awf-plans/{workspace_id}.md",
                    "conformance_report_path": "docs/awf-plans/{workspace_id}.conformance.json",
                    "max_iterations": 0,
                    "fail_on_unexplained_deviation": False,
                },
            }
        )

        fake.queue_result(returncode=0, stdout="")  # before planning changed paths
        fake.queue_result(returncode=0, stdout="sha_pre\n")  # rev-parse HEAD baseline
        # Adapter writes the plan file, but git status and committed diff do
        # not surface it. The digest-change check must accept the on-disk plan
        # and allow execution to proceed.
        fake.queue_result(returncode=0, stdout="")  # changed paths after planning
        fake.queue_result(returncode=0, stdout="")  # committed paths since planning baseline
        fake.queue_result(returncode=0, stdout="sha_post\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout=f"?? {plan_path.as_posix()}\n")
        fake.queue_result(returncode=0, stdout="sha_post\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=f"?? {plan_path.as_posix()}\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
        fake.queue_result(returncode=0, stdout="sha_post\n")  # rev-parse HEAD iter post

        result = await executor._run_agent_task_with_optional_planning(
            adapter=_PlanWritingAdapter(runner=fake),
            workspace=workspace,
            profile=profile,
            compose_project=f"awf_{ws_id}",
            compose_file=Path("docker-compose.yml"),
            worktree_path=worktree_path,
            model=None,
            command_evidence=[],
            accept_existing_plan=True,
        )

        assert result is None

    @pytest.mark.unit
    async def test_planning_validation_handoff_rejects_empty_diff_without_baseline(
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
                    "max_iterations": 0,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )

        plan_path = f"docs/awf-plans/{ws_id}.md"
        report_path = f"docs/awf-plans/{ws_id}.conformance.json"
        handoff_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Implementation appears complete; AWF validation evidence is missing.",
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": [
                    {
                        "kind": "awf_validation_evidence",
                        "detail": "AWF-owned validation evidence is missing for pytest.",
                    }
                ],
            }
        )

        fake.queue_result(returncode=0, stdout="")  # before planning changed paths
        fake.queue_result(returncode=0, stdout="sha_pre\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning adapter
        fake.queue_result(returncode=0, stdout=f"?? {plan_path}\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since planning baseline
        fake.queue_result(returncode=1, stdout="")  # rev-parse HEAD pre-loop unavailable
        fake.queue_result(returncode=0, stdout="implemented")  # execution adapter
        fake.queue_result(returncode=0, stdout=f"?? {plan_path}\n")  # before compare
        fake.queue_result(returncode=0, stdout="sha_compare\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=handoff_report)  # conformance handoff request
        fake.queue_result(returncode=0, stdout=f"?? {plan_path}\n?? {report_path}\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
        fake.queue_result(returncode=0, stdout="sha_compare\n")  # rev-parse HEAD iter post

        await executor.execute(ws_id)

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            events = list(workspace.events)

        assert workspace.status == WorkspaceStatus.failed.value
        assert not any(event.reason_code == CONFORMANCE_REQUIRES_AWF_VALIDATION for event in events)

    @pytest.mark.unit
    async def test_preserved_conformance_retry_scope_allows_clean_retry(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        tmp_path: Path,
    ) -> None:
        baseline = {
            "conformance_before_compare": set(),
            "conformance_before_compare_head": None,
            "conformance_before_dirty_digests": {},
        }
        fake.queue_result(returncode=0, stdout="")  # changed paths after resumed conformance

        from awf.control.executor import planning_ops as executor_planning_ops

        result = await executor_planning_ops._check_preserved_conformance_retry_scope(
            executor,
            worktree_path=tmp_path,
            report_path=Path("docs/awf-plans/ws.conformance.json"),
            planning_retry_scope_baseline=baseline,
        )

        assert result is None

    @pytest.mark.unit
    async def test_planning_profile_records_conformance_stall_when_compare_idle_timeout_after_implementation_commits(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.adapters import base as adapter_base
        from awf.adapters.base import AgentRunResult
        from awf.common.commands import CommandResult
        from awf.control.executor import planning_ops as executor_planning_ops
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

        monkeypatch.setattr(executor_planning_ops, "_monotonic", _fake_monotonic)

        class _IdleConformanceAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.codex

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            def get_provider(self, model: str | None) -> str:
                return "openai"

            def _cli_args(self, *, model: str | None) -> list[str]:
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

        monkeypatch.setitem(adapter_base._REGISTRY, AgentRuntime.codex, _IdleConformanceAdapter)

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
        fake.queue_result(
            returncode=0, stdout=""
        )  # changed_paths after planning (plan committed, not dirty)
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
        fake.queue_result(returncode=0, stdout="sha_post\n")  # conformance scope HEAD
        # Conformance adapter raises AgentRunError; executor still recomputes
        # after_compare so the fail_on_unexplained_deviation scope check
        # applies on the timeout branch (no extra paths here), then captures
        # HEAD for the iteration-end progress digest.
        fake.queue_result(  # after_compare git status (post-timeout)
            returncode=0,
            stdout=" M src/awf/foo.py\n",
        )
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
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
            call for call in fake.calls if "rev-list" in call.args and "--count" in call.args
        ]
        assert len(revlist_calls) == 1
        assert "sha_post..HEAD" in revlist_calls[0].args
        revlist_index = fake.calls.index(revlist_calls[0])
        post_stall_diff = [
            call
            for call in fake.calls[revlist_index + 1 :]
            if "diff" in call.args and "--name-only" in call.args and "sha_post..HEAD" in call.args
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
        from awf.adapters import base as adapter_base
        from awf.adapters.base import AgentRunResult
        from awf.common.commands import CommandResult
        from awf.control.executor import planning_ops as executor_planning_ops
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

        monkeypatch.setattr(executor_planning_ops, "_monotonic", _fake_monotonic)

        class _IdleConformanceAdapter(adapter_base.AgentAdapter):
            runtime = AgentRuntime.codex

            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            def get_provider(self, model: str | None) -> str:
                return "openai"

            def _cli_args(self, *, model: str | None) -> list[str]:
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

        monkeypatch.setitem(adapter_base._REGISTRY, AgentRuntime.codex, _IdleConformanceAdapter)

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
            f"?? docs/awf-plans/{ws_id}.md\n?? docs/awf-plans/{ws_id}.conformance.json\n"
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
        fake.queue_result(returncode=0, stdout="sha_pre\n")  # conformance scope HEAD
        # conformance adapter raises AgentRunError
        fake.queue_result(  # after_compare git status (post-timeout, unchanged)
            returncode=0,
            stdout=plan_plus_stale_status,
        )
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
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
        fake.queue_result(returncode=0, stdout="sha1\n")  # conformance scope HEAD
        fake.queue_result(  # compare says not done (different summary each time)
            returncode=0,
            stdout='{"status":"needs_iteration","summary":"gap-1","gaps":["add tests"]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
        fake.queue_result(returncode=0, stdout="fixed gap")  # iteration execute
        fake.queue_result(
            returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n M src/y.py\n"
        )
        fake.queue_result(returncode=0, stdout="sha1\n")  # conformance scope HEAD
        fake.queue_result(  # compare satisfied
            returncode=0,
            stdout='{"status":"satisfied","summary":"done","gaps":[]}',
        )
        fake.queue_result(
            returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n M src/y.py\n"
        )
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
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
            assert ws.subphase == "validation"
            assert ws.last_activity_at is not None
            failed_events = [
                event for event in ws.events if event.reason_code == AGENT_STALLED_IN_CONFORMANCE
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
        from awf.control.executor import planning_ops as executor_planning_ops
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

        monkeypatch.setattr(executor_planning_ops, "_monotonic", _fake_monotonic)

        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # initial execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # conformance scope HEAD
        fake.queue_result(
            returncode=0,
            stdout='{"status":"needs_iteration","summary":"gap-1","gaps":["add tests"]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
        fake.queue_result(returncode=0, stdout="fixed gap")  # iteration execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/y.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # conformance scope HEAD
        fake.queue_result(
            returncode=0,
            stdout='{"status":"satisfied","summary":"done","gaps":[]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/y.py\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
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
            assert ws.subphase == "validation"
            assert ws.last_activity_at is not None
            assert [
                event for event in ws.events if event.reason_code == AGENT_STALLED_IN_CONFORMANCE
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
            fake.queue_result(returncode=0, stdout="sha1\n")  # conformance scope HEAD
            fake.queue_result(returncode=0, stdout=identical_report)  # conformance adapter
            fake.queue_result(returncode=0, stdout=identical_paths)  # after_compare
            fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
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
            fake.queue_result(returncode=0, stdout=f"{sha}\n")  # conformance scope HEAD
            fake.queue_result(returncode=0, stdout=identical_report)  # conformance
            fake.queue_result(returncode=0, stdout=clean_paths)  # after_compare
            fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
            fake.queue_result(returncode=0, stdout=f"{sha}\n")  # rev-parse HEAD iter post
            fake.queue_result(
                returncode=0,
                stdout="src/x.py\n",
            )  # committed implementation paths since post-planning HEAD

        # Fourth iteration: agent finally satisfies the plan (commits + report flips)
        fake.queue_result(returncode=0, stdout="execute output")  # execute
        fake.queue_result(returncode=0, stdout=clean_paths)  # before_compare
        fake.queue_result(returncode=0, stdout="sha_iter3\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=satisfied_report)  # conformance
        fake.queue_result(returncode=0, stdout=clean_paths)  # after_compare
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
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
            assert ws.subphase == "validation"
            assert ws.last_activity_at is not None
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
        from awf.control.executor import planning_ops as executor_planning_ops
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

        monkeypatch.setattr(executor_planning_ops, "_monotonic", _fake_monotonic)

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
        fake.queue_result(returncode=0, stdout="base_sha\n")  # conformance scope HEAD
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
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
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
            assert ws.subphase == "validation"
            assert ws.last_activity_at is not None
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
        assert _json_value(run["commands"]) == [
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
        identity_inputs = _json_value(run["environment_identity_inputs"])
        assert identity_inputs["schema_version"] == 1
        assert "runtime" in identity_inputs
        assert run["status"] == "succeeded"
        assert run["reason_code"] == "VALIDATION_OK"
        assert run["started_at"] is not None
        assert run["finished_at"] is not None
        assert _json_value(run["log_stream_refs"]) == {
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
        assert _json_value(operations["payload"])["requested_tier"] == 2
        assert _json_value(operations["result"])["requested_tier"] == 2
