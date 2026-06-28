"""Executor tests with FakeCommandRunner + PostgreSQL.

Each test drives one workspace through the full pipeline with canned
subprocess output. The single runner handles all compose/adapter/pr calls
since each call is distinguishable by its argv.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.adapters.base import AgentAdapter
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    ollama_model,
)
from awf.control.executor import execution_validation as execution_validation_mod
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
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


class TestHappyPathPart001:
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
            assert persisted.execution_claim_expires_at == lease_expires_at

    @pytest.mark.unit
    async def test_claim_ready_worker_restart_recovery_rejects_other_live_execution_claim(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        previous_expiry = datetime.now(UTC) + timedelta(minutes=5)
        ws_id = await _seed_running_worker_restart_recovery(
            factory,
            execution_claimed_by="worker-a",
            execution_claim_expires_at=previous_expiry,
        )

        ws = await executor._claim_ready(
            ws_id,
            execution_owner_id="worker-b",
            execution_lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

        assert ws is None
        async with factory() as s:
            persisted = await WorkspaceRepository(s).get(ws_id)
            assert persisted is not None
            assert persisted.status == WorkspaceStatus.running.value
            assert persisted.execution_claimed_by == "worker-a"
            assert persisted.execution_claim_expires_at == previous_expiry

    @pytest.mark.unit
    async def test_claim_ready_worker_restart_recovery_refreshes_same_execution_owner(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_running_worker_restart_recovery(
            factory,
            execution_claimed_by="worker-a",
            execution_claim_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        refreshed_expiry = datetime.now(UTC) + timedelta(minutes=10)

        ws = await executor._claim_ready(
            ws_id,
            execution_owner_id="worker-a",
            execution_lease_expires_at=refreshed_expiry,
        )

        assert ws is not None
        async with factory() as s:
            persisted = await WorkspaceRepository(s).get(ws_id)
            assert persisted is not None
            assert persisted.execution_claimed_by == "worker-a"
            assert persisted.execution_claim_expires_at == refreshed_expiry

    @pytest.mark.unit
    async def test_claim_ready_worker_restart_recovery_claims_stale_execution_claim(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_running_worker_restart_recovery(
            factory,
            execution_claimed_by="worker-a",
            execution_claim_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        ws = await executor._claim_ready(
            ws_id,
            execution_owner_id="worker-b",
            execution_lease_expires_at=lease_expires_at,
        )

        assert ws is not None
        async with factory() as s:
            persisted = await WorkspaceRepository(s).get(ws_id)
            assert persisted is not None
            assert persisted.execution_claimed_by == "worker-b"
            assert persisted.execution_claim_expires_at == lease_expires_at

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "workspace_status",
        [WorkspaceStatus.validating, WorkspaceStatus.pushing],
    )
    async def test_claim_ready_worker_restart_recovery_rejects_non_running_inflight_claim(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
        workspace_status: WorkspaceStatus,
    ) -> None:
        ws_id = await _seed_running_worker_restart_recovery(
            factory,
            workspace_status=workspace_status,
        )
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        ws = await executor._claim_ready(
            ws_id,
            execution_owner_id="worker-b",
            execution_lease_expires_at=lease_expires_at,
        )

        assert ws is None
        async with factory() as s:
            persisted = await WorkspaceRepository(s).get(ws_id)
            assert persisted is not None
            assert persisted.status == workspace_status.value
            assert persisted.execution_claimed_by is None
            assert persisted.execution_claim_expires_at is None

    @pytest.mark.unit
    async def test_claim_ready_worker_restart_recovery_claims_unset_execution_claim(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_running_worker_restart_recovery(factory)
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        ws = await executor._claim_ready(
            ws_id,
            execution_owner_id="worker-b",
            execution_lease_expires_at=lease_expires_at,
        )

        assert ws is not None
        async with factory() as s:
            persisted = await WorkspaceRepository(s).get(ws_id)
            assert persisted is not None
            assert persisted.execution_claimed_by == "worker-b"
            assert persisted.execution_claim_expires_at == lease_expires_at

    @pytest.mark.unit
    async def test_claim_ready_worker_restart_recovery_requires_real_execution_lease(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_running_worker_restart_recovery(factory)

        ws = await executor._claim_ready(ws_id)

        assert ws is None
        async with factory() as s:
            persisted = await WorkspaceRepository(s).get(ws_id)
            assert persisted is not None
            assert persisted.execution_claimed_by is None
            assert persisted.execution_claim_expires_at is None

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
            assert ws.subphase == "validation"
            assert ws.last_activity_at is not None
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
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The pre-agent Ollama preflight probes the host daemon, which is
        # unreachable under test; stub it so the cloud model is treated as
        # served remotely and the adapter actually runs (issue #552).
        monkeypatch.setattr(
            ollama_model,
            "ensure_ollama_model_available",
            lambda **_kwargs: {"status": "ok", "reason_code": "OLLAMA_MODEL_CLOUD"},
        )
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
    async def test_cursor_lower_effort_without_model_override_omits_thinking_model(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Verify lower Cursor effort does not force the thinking model."""
        ws_id = await _seed_ready_workspace(
            factory,
            agent="cursor",
            task_policy={"agent_effort": "medium"},
        )

        fake.queue_result(returncode=0, stdout="cursor finished")  # adapter
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
            stdout="https://github.com/dimileeh/aira-agent/pull/126\n",
        )  # gh pr create

        await executor.execute(ws_id)

        adapter_args = fake.calls[0].args
        cursor_start = adapter_args.index("cursor-agent")
        assert adapter_args[cursor_start:] == [
            "cursor-agent",
            "-p",
            "--force",
            "--output-format",
            "text",
        ]
        assert "-m" not in adapter_args[cursor_start:]

    @pytest.mark.unit
    async def test_pr_monitor_receives_adapter_bound_to_workspace_model(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The pre-agent Ollama preflight probes the host daemon, which is
        # unreachable under test; stub it so the cloud model is treated as
        # served remotely and the adapter/monitor actually run (issue #552).
        monkeypatch.setattr(
            ollama_model,
            "ensure_ollama_model_available",
            lambda **_kwargs: {"status": "ok", "reason_code": "OLLAMA_MODEL_CLOUD"},
        )
        captured: list[tuple[str | None, str | None, str | None]] = []

        class Monitor:
            async def run(self, **_: object) -> None:
                return None

        def monitor_factory(
            adapter: AgentAdapter,
            *_: object,
            provider_recovery_default_model: str | None = None,
        ) -> Monitor:
            captured.append(
                (
                    adapter._default_model,
                    adapter._default_effort,
                    provider_recovery_default_model,
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

        assert captured == [("ollama/glm-5.1:cloud", "xhigh", "ollama/kimi-k2.6:cloud")]

    @pytest.mark.unit
    async def test_planning_profile_runs_plan_execute_compare_before_validation(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
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

        # The planning + conformance adapters are faked, so seed the worktree
        # plan + conformance report files the real agent would write; the
        # deposit step surfaces them into the served artifact dir.
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n\n- implement foo\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "satisfied", "summary": "plan achieved", "gaps": []}',
            encoding="utf-8",
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

        adapter_prompts = _adapter_prompts(fake)
        assert len(adapter_prompts) == 3
        assert "Planning phase" in adapter_prompts[0]
        assert "Execution phase" in adapter_prompts[1]
        assert "Conformance phase" in adapter_prompts[2]

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.subphase == "validation"
            assert ws.last_activity_at is not None

        # The plan + conformance report were deposited into the served artifact
        # dir (a sibling of the worktree) before teardown, so the console can
        # surface them by stable name.
        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").read_text(encoding="utf-8").startswith("# Plan")
        assert (served_dir / "conformance.json").is_file()

    @pytest.mark.unit
    async def test_planning_validation_handoff_runs_validation_then_conformance_only_check(
        self,
        monkeypatch: pytest.MonkeyPatch,
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
                    "max_iterations": 2,
                    "enforce_plan_only_changes": True,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )

        handoff_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Implementation appears complete; AWF validation evidence is missing.",
                "reason_code": CONFORMANCE_REQUIRES_AWF_VALIDATION,
                "gaps": ["AWF-owned validation evidence is missing for the pytest gate."],
            }
        )
        satisfied_report = json.dumps(
            {
                "status": "satisfied",
                "summary": "implementation and validation satisfy plan",
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
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=handoff_report)  # conformance handoff
        fake.queue_result(
            returncode=0,
            stdout=(
                f"?? docs/awf-plans/{ws_id}.md\n"
                f"?? docs/awf-plans/{ws_id}.conformance.json\n"
                " M src/x.py\n"
            ),
        )
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
        fake.queue_result(returncode=0, stdout="base_commit_sha\n")  # rev-parse HEAD post-iter
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="src/x.py\n")  # cached diff
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor
        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=satisfied_report)  # conformance-only rerun
        report_path = f"docs/awf-plans/{ws_id}.conformance.json"
        fake.queue_result(returncode=0, stdout=f"?? {report_path}\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD
        fake.queue_result(returncode=0, stdout="")  # git restore report path
        fake.queue_result(returncode=0, stdout="")  # post-restore cleanliness check
        _queue_pre_push_diagnostics(fake)
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        recovery_calls: list[dict[str, object]] = []
        original_recovery = execution_validation_mod._run_agent_callable_with_service_recovery

        async def _spy_recovery(*args: object, **kwargs: object) -> tuple[bool, object]:
            recovery_calls.append(kwargs)
            return await original_recovery(*args, **kwargs)

        monkeypatch.setattr(
            execution_validation_mod,
            "_run_agent_callable_with_service_recovery",
            _spy_recovery,
        )

        await executor.execute(ws_id)

        adapter_prompt_calls = _adapter_prompt_calls(fake)
        prompts = [prompt for _, prompt in adapter_prompt_calls]
        validation_call_index = next(
            index
            for index, call in enumerate(fake.calls)
            if "pytest -q" in call.args[-1] and "codex" not in call.args
        )
        conformance_call_indexes = [
            index for index, prompt in adapter_prompt_calls if "Conformance phase" in prompt
        ]
        phase_names = []
        for prompt in prompts:
            if "## Planning phase" in prompt:
                phase_names.append("planning")
            elif "## Execution phase" in prompt:
                phase_names.append("execution")
            elif "## Conformance phase" in prompt:
                phase_names.append("conformance")

        assert phase_names == ["planning", "execution", "conformance", "conformance"]
        assert conformance_call_indexes[-1] > validation_call_index
        assert recovery_calls
        assert recovery_calls[-1]["expected_status"] is WorkspaceStatus.validating
        assert recovery_calls[-1]["failure_from_status"] is WorkspaceStatus.validating
        assert callable(recovery_calls[-1]["before_agent_retry"])
        assert callable(recovery_calls[-1]["after_agent_cleanup_failure_repair"])
        assert "Validation evidence" in prompts[-1]
        assert "VALIDATION_OK" in prompts[-1]
        assert "validation.01_validate.stdout" in prompts[-1]
        # #544: the satisfied report is written but never staged or committed
        # (its path is gitignored), so no git add/commit of the report occurs.
        git_calls = [call.args for call in fake.calls if call.args and call.args[0] == "git"]
        assert not any(call[-3:] == ["add", "--", report_path] for call in git_calls)
        assert not any(
            "commit" in call and "awf: post-validation conformance report" in call
            for call in git_calls
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id, limit=20)
            runs = (
                (
                    await s.execute(
                        text(
                            "SELECT status, reason_code, log_stream_refs "
                            "FROM validation_runs WHERE workspace_id = :workspace_id"
                        ),
                        {"workspace_id": ws_id},
                    )
                )
                .mappings()
                .all()
            )

        assert ws.status == WorkspaceStatus.completed.value
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["reason_code"] == "VALIDATION_OK"
        assert _json_value(runs[0]["log_stream_refs"]) == {
            "commands": [
                {
                    "stdout": "validation.01_validate.stdout",
                    "stderr": "validation.01_validate.stderr",
                }
            ]
        }
        handoff_events = [
            event for event in events if event.reason_code == CONFORMANCE_REQUIRES_AWF_VALIDATION
        ]
        assert handoff_events

    @pytest.mark.unit
    async def test_validation_handoff_evidence_prefers_coverage_column_and_redacts(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)

        async with factory() as session:
            repo = ValidationRunRepository(session)
            run = await repo.start(
                workspace_id=ws_id,
                attempt_id=None,
                tier=1,
                commands=[
                    {
                        "phase": "validate",
                        "command": "GITHUB_TOKEN=ghp_secretvalue123 pytest -q",
                    }
                ],
                base_commit="base",
                target_branch="main",
                target_head_sha="target",
                log_stream_refs={
                    "coverage": {
                        "status": "failed",
                        "reason_code": "COVERAGE_BELOW_THRESHOLD",
                        "percent": 72.0,
                    }
                },
            )
            await repo.finish(
                run.id,
                status="succeeded",
                reason_code="VALIDATION_OK",
                coverage={
                    "status": "passed",
                    "reason_code": "COVERAGE_OK",
                    "percent": 99.4,
                },
            )
            run.log_stream_refs = {
                "coverage": {
                    "status": "failed",
                    "reason_code": "COVERAGE_BELOW_THRESHOLD",
                    "percent": 72.0,
                }
            }
            await session.commit()
            validation_run_id = run.id

        evidence = await executor._validation_run_evidence_for_conformance(validation_run_id)

        assert "COVERAGE_OK" in evidence
        assert "COVERAGE_BELOW_THRESHOLD" not in evidence
        assert "ghp_secretvalue123" not in evidence
        assert "[redacted]" in evidence

    @pytest.mark.unit
    async def test_validation_handoff_evidence_keeps_late_coverage_command_provenance(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        commands = [
            {"phase": "validate", "command": f"pytest tests/unit/test_{idx}.py -q"}
            for idx in range(24)
        ]
        commands.append(
            {
                "phase": "coverage",
                "command": "pytest --cov=awf --cov-report=term",
            }
        )

        async with factory() as session:
            repo = ValidationRunRepository(session)
            run = await repo.start(
                workspace_id=ws_id,
                attempt_id=None,
                tier=1,
                commands=commands,
                base_commit="base",
                target_branch="main",
                target_head_sha="target",
                log_stream_refs={},
            )
            await repo.finish(
                run.id,
                status="succeeded",
                reason_code="VALIDATION_OK",
                coverage={
                    "status": "passed",
                    "reason_code": "COVERAGE_OK",
                    "percent": 99.4,
                },
                coverage_evidence_status="reused",
                coverage_evidence_reason_code="COVERAGE_EVIDENCE_REUSED",
            )
            validation_run_id = run.id
            await session.commit()

        evidence = await executor._validation_run_evidence_for_conformance(validation_run_id)
        json_text = evidence.split("```json\n", 1)[1].split("\n```", 1)[0]
        payload = json.loads(json_text)
        coverage_commands = [
            command for command in payload["commands"] if command.get("phase") == "coverage"
        ]

        assert len(payload["commands"]) == 25
        assert coverage_commands == [
            {
                "phase": "coverage",
                "command": "pytest --cov=awf --cov-report=term",
                "evidence_status": "reused",
                "evidence_reason_code": "COVERAGE_EVIDENCE_REUSED",
            }
        ]

    @pytest.mark.unit
    async def test_validation_handoff_evidence_keeps_large_payload_json_valid(
        self,
        executor: WorkspaceExecutor,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)

        async with factory() as session:
            repo = ValidationRunRepository(session)
            run = await repo.start(
                workspace_id=ws_id,
                attempt_id=None,
                tier=1,
                commands=[
                    {
                        "phase": "validate",
                        "command": f"pytest tests/unit/test_{idx}.py " + ("x" * 1500),
                    }
                    for idx in range(25)
                ],
                base_commit="base",
                workspace_head_sha="workspace-head",
                target_branch="main",
                target_head_sha="target",
                log_stream_refs={
                    f"stream_{idx:02d}": {
                        "stdout": f"validation.{idx:02d}.stdout",
                        "stderr": "stderr-" + ("y" * 1500),
                    }
                    for idx in range(40)
                },
            )
            await repo.finish(
                run.id,
                status="succeeded",
                reason_code="VALIDATION_OK",
                coverage={
                    "status": "passed",
                    "reason_code": "COVERAGE_OK",
                    "percent": 99.4,
                },
            )
            validation_run_id = run.id
            await session.commit()

        evidence = await executor._validation_run_evidence_for_conformance(validation_run_id)
        json_text = evidence.split("```json\n", 1)[1].split("\n```", 1)[0]
        payload = json.loads(json_text)
        keys = list(payload)

        assert len(json_text) <= 20000
        assert keys.index("coverage") < keys.index("commands")
        assert keys.index("workspace_head_sha") < keys.index("commands")
        assert payload["status"] == "succeeded"
        assert payload["reason_code"] == "VALIDATION_OK"
        assert payload["coverage"]["reason_code"] == "COVERAGE_OK"
        assert payload["workspace_head_sha"] == "workspace-head"

    @pytest.mark.unit
    async def test_planning_validation_handoff_agent_failure_finishes_validate_operation(
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
            operation_id="op_validate_handoff_agent_failed",
        )

        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # conformance scope HEAD
        fake.queue_result(returncode=1, stderr="conformance runner failed")

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
                            WHERE id = 'op_validate_handoff_agent_failed'
                            """
                        )
                    )
                )
                .mappings()
                .one()
            )

        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "agent_failure"
        assert "post-validation conformance agent failed" in (ws.failure_message or "")
        assert run == {"status": "succeeded", "reason_code": "VALIDATION_OK"}
        assert operation["status"] == "failed"
        assert operation["error_code"] == "AGENT_CLI_FAILED"
        assert operation["finished_at"] is not None
        assert "post-validation conformance agent failed" in operation["error_message"]
        result = _json_value(operation["result"])
        assert result["reason_code"] == "AGENT_CLI_FAILED"
        assert result["validation_run_id"]

    @pytest.mark.unit
    async def test_post_validation_conformance_gap_stops_at_preserved_handoff_budget(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
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
        # Seed the worktree plan + (unsatisfied) conformance report the real
        # agent would write; the deposit must surface them even on the
        # preserved-FAILED stop path so the console stays uniform.
        worktree_plans = _test_worktrees_root(factory) / ws_id / "docs" / "awf-plans"
        worktree_plans.mkdir(parents=True, exist_ok=True)
        (worktree_plans / f"{ws_id}.md").write_text("# Plan\n", encoding="utf-8")
        (worktree_plans / f"{ws_id}.conformance.json").write_text(
            '{"status": "needs_iteration", "gaps": ["incomplete"]}',
            encoding="utf-8",
        )
        operation_id = "op_post_validation_conformance_gap"
        await _insert_validate_handoff_recovery_operation(
            factory,
            workspace_id=ws_id,
            operation_id=operation_id,
            requested_tier=1,
            conformance_overrides={"iteration": 1, "max_iterations": 2},
        )
        report_path = f"docs/awf-plans/{ws_id}.conformance.json"
        post_validation_gap_report = json.dumps(
            {
                "status": "needs_iteration",
                "summary": "Validation passed, but the API docs are still incomplete.",
                "gaps": ["Document the API endpoint required by the saved plan."],
            }
        )

        _queue_validation_head(fake)
        fake.queue_result(returncode=0, stdout="tests ok")  # validation
        fake.queue_result(returncode=0, stdout="")  # post-validation conformance before status
        fake.queue_result(returncode=0, stdout="deadbeef01\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=post_validation_gap_report)
        fake.queue_result(
            returncode=0,
            stdout=f"?? {report_path}\n",
        )
        fake.queue_result(returncode=0, stdout="")  # committed paths since scope HEAD

        await executor.execute(ws_id)

        adapter_prompts = _adapter_prompts(fake)
        post_validation_conformance_prompts = [
            prompt
            for prompt in adapter_prompts
            if "Conformance phase" in prompt and "### Validation evidence" in prompt
        ]

        assert len(adapter_prompts) == 1
        assert post_validation_conformance_prompts == adapter_prompts
        assert [
            line
            for prompt in post_validation_conformance_prompts
            for line in prompt.splitlines()
            if line.startswith("Iteration: ")
        ] == ["Iteration: 2"]
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
            operation = (
                (
                    await s.execute(
                        text(
                            """
                            SELECT status, error_code, result, finished_at, payload,
                                   idempotency_key
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
            extra_validate_recovery_ops = (
                await s.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM operations
                        WHERE workspace_id = :workspace_id
                          AND type = 'validate'
                          AND status IN ('pending', 'running')
                          AND id <> :operation_id
                          AND idempotency_key LIKE 'pr_monitor:validate_only:%'
                        """
                    ),
                    {"workspace_id": ws_id, "operation_id": operation_id},
                )
            ).scalar_one()

        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "agent_failure"
        assert "Document the API endpoint required by the saved plan." in (ws.failure_message or "")
        assert [run["status"] for run in runs] == ["succeeded"]
        assert [run["reason_code"] for run in runs] == ["VALIDATION_OK"]
        assert operation["status"] == "failed"
        assert operation["error_code"] == PLAN_CONFORMANCE_UNSATISFIED
        assert operation["finished_at"] is not None
        payload = _json_value(operation["payload"])
        assert payload["owner"] == "pr_monitor"
        assert payload["source"] == "pr_monitor"
        assert payload["action"] == "validate_only"
        assert payload["requested_action"] == "validate"
        assert payload["requested_tier"] == 1
        assert payload["source_head_sha"] == "deadbeef01"
        assert payload["source_base_sha"] == "a" * 40
        assert payload["target_branch"] == "development"
        assert payload["remote_branch"] == f"awf/{ws_id}"
        assert payload["recovery_mode"] == "validate_only"
        assert payload["conformance"]["iteration"] == 1
        assert payload["conformance"]["max_iterations"] == 2
        assert operation["idempotency_key"].startswith("pr_monitor:validate_only:")
        result = _json_value(operation["result"])
        assert result["reason_code"] == PLAN_CONFORMANCE_UNSATISFIED
        assert result["requested_tier"] == 1
        assert extra_validate_recovery_ops == 0

        # Preserved FAILED workspace still surfaces its plan + (unsatisfied)
        # conformance report in the served artifact dir.
        served_dir = tmp_path / "work" / "artifacts" / ws_id
        assert (served_dir / "plan.md").is_file()
        assert (served_dir / "conformance.json").read_text(
            encoding="utf-8"
        ) == '{"status": "needs_iteration", "gaps": ["incomplete"]}'
