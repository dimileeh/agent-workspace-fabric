"""Service-level retry/requeue tests for terminal workspaces."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateV2Request
from awf.db.base import Base
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.models import Operation, Task, TaskAttempt, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
)
from awf.service.workspaces import WorkspaceRetryNotFoundError, WorkspaceService


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _request(*, task_kind: str = "feature_branch_pr") -> WorkspaceCreateV2Request:
    return WorkspaceCreateV2Request(
        repo={"url": "git@github.com:example/retryable.git", "base_branch": "development"},
        task={
            "title": "Retry flaky validation",
            "prompt": "Fix the intermittent validation failure.",
            "agent": "codex",
            "kind": task_kind,
            "external_id": "TICKET-RETRY",
            "task_class": "test_task",
            "owned_paths": ["src/awf/retry/**"],
            "auto_merge": False,
            "initial_review_grace_period_seconds": 30,
        },
        workspace={"profile_ref": "python", "profile": None},
        validation={"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        resources={},
    )


@pytest.mark.unit
def test_retry_not_found_error_has_instance_detail() -> None:
    error = WorkspaceRetryNotFoundError("ws_missing")

    assert error.detail is None
    assert error.__dict__["detail"] is None


async def _mark_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    branch_name: str = "codex/old-attempt",
    remote_push_branch: str | None = None,
) -> dict[str, object]:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = "validation_failure"
        workspace.failure_message = "pytest failed"
        workspace.branch_name = branch_name
        workspace.remote_push_branch = remote_push_branch
        workspace.pr_url = "https://github.com/example/retryable/pull/10"
        workspace.compose_project_name = "awf_old_attempt"
        assert workspace.resolved_profile is not None
        frozen_profile = {
            **workspace.resolved_profile,
            "source": "frozen:test-profile",
        }
        workspace.resolved_profile = frozen_profile
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await session.commit()
        return frozen_profile


async def _mark_conformance_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = (
            "plan conformance was not satisfied after 0 iteration(s): add tests"
        )
        workspace.branch_name = "awf/ws_old"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            payload={
                "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                "details": {
                    "conformance": {
                        "summary": "Implementation is incomplete.",
                        "gaps": ["Add regression test", "Wire retry endpoint"],
                        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                        "iterations_used": 0,
                        "max_iterations": 0,
                        "plan_path": "docs/awf-plans/ws_old.md",
                        "report_path": "docs/awf-plans/ws_old.conformance.json",
                    }
                },
                "salvage": {
                    "hint": "Workspace worktree and branch were preserved for salvage.",
                    "worktree_path": "/worktrees/ws_old",
                    "branch_name": "awf/ws_old",
                    "remote_push_branch": "awf/ws_old",
                },
            },
        )
        await session.commit()


async def _mark_conformance_failed_without_evidence(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "plan conformance was not satisfied"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            payload={
                "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                "details": {"conformance": "legacy-invalid"},
            },
        )
        await session.commit()


async def _mark_planning_scope_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    approved_fallback_model: str | None = None,
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = (
            "planning phase changed files outside `docs/awf-plans/ws_scope_old.md`"
        )
        workspace.branch_name = "awf/ws_scope_old"
        workspace.remote_push_branch = "awf/ws_scope_old"
        workspace.task_policy = {
            **workspace.task_policy,
            **(
                {
                    "planning_scope_recovery": {
                        "approved_fallback_model": approved_fallback_model,
                    }
                }
                if approved_fallback_model is not None
                else {}
            ),
        }
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
            payload={
                "reason_code": AGENT_PLAN_PHASE_SCOPE_VIOLATION,
                "message": workspace.failure_message,
                "details": {
                    "planning_scope": {
                        "scope_phase": "planning",
                        "required_paths": ["docs/awf-plans/ws_scope_old.md"],
                        "offending_paths": ["src/awf/runtime/planning.py"],
                        "offending_commands": [],
                        "recommended_action": (
                            "Retry planning from a clean workspace and salvage the "
                            "preserved branch only after explicit operator approval."
                        ),
                        "recovery_strategy": "discard_and_replan",
                        "salvage_policy": "explicit_salvage_required",
                    },
                    "recommended_action": (
                        "Retry planning from a clean workspace and salvage the preserved "
                        "branch only after explicit operator approval."
                    ),
                    "recovery_strategy": "discard_and_replan",
                    "salvage_policy": "explicit_salvage_required",
                },
                "salvage": {
                    "hint": "Workspace worktree and branch were preserved for salvage.",
                    "worktree_path": "/worktrees/ws_scope_old",
                    "branch_name": "awf/ws_scope_old",
                    "remote_push_branch": "awf/ws_scope_old",
                },
            },
        )
        await session.commit()


@pytest.mark.unit
async def test_retry_failed_workspace_clones_v2_metadata_and_increments_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    frozen_profile = await _mark_failed(factory, first.id)

    retry = await service.retry_workspace(first.id)

    async with factory() as session:
        original = await WorkspaceRepository(session).get(first.id)
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        tasks = list((await session.execute(select(Task))).scalars())
        attempts = list(
            (
                await session.execute(
                    select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                )
            ).scalars()
        )
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retried.id)
                )
            ).scalars()
        )
        retry_events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.event_type.in_(
                            ["workspace.retry_requested", "workspace.retry_created"]
                        )
                    )
                )
            ).scalars()
        )

    assert original is not None
    assert retried is not None
    assert retry.source_workspace_id == first.id
    assert retry.new_workspace_id != first.id
    assert retry.status == WorkspaceStatus.requested
    assert retry.attempt_number == 2

    assert retried.status == WorkspaceStatus.requested.value
    assert retried.repo_url == original.repo_url
    assert retried.branch_base == original.branch_base
    assert retried.task_title == original.task_title
    assert retried.task_prompt == original.task_prompt
    assert retried.task_external_id == original.task_external_id
    assert retried.task_class == original.task_class
    assert retried.owned_paths == original.owned_paths
    assert retried.auto_merge is False
    assert retried.initial_review_grace_period_seconds == 30
    assert retried.agent == AgentRuntime.codex.value
    assert retried.profile_ref == "python"
    assert retried.resolved_profile == frozen_profile
    assert retried.test_commands == ["uv run pytest tests/unit -q"]
    assert retried.failure_reason is None
    assert retried.failure_message is None
    assert retried.pr_url is None
    assert retried.compose_project_name is None

    assert len(tasks) == 1
    assert [attempt.workspace_id for attempt in attempts] == [first.id, retried.id]
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert {attempt.task_id for attempt in attempts} == {tasks[0].id}

    assert len(operations) == 1
    assert operations[0].workspace_id == retried.id
    assert operations[0].type == "retry"
    assert operations[0].status == "succeeded"
    assert operations[0].payload == {"source_workspace_id": first.id}
    assert operations[0].result == {
        "new_workspace_id": retried.id,
        "attempt_number": 2,
        "status": "requested",
    }

    assert {
        (event.workspace_id, event.event_type, event.payload["source_workspace_id"])
        for event in retry_events
        if event.payload is not None
    } == {
        (first.id, "workspace.retry_requested", first.id),
        (retried.id, "workspace.retry_created", first.id),
    }


@pytest.mark.unit
async def test_retry_conformance_unsatisfied_enriches_prompt_with_final_gaps(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    await _mark_conformance_failed(factory, first.id)

    retry = await service.retry_workspace(first.id)

    async with factory() as session:
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retry.new_workspace_id)
                )
            ).scalars()
        )
        retry_created = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == retry.new_workspace_id,
                        WorkspaceEvent.event_type == "workspace.retry_created",
                    )
                )
            ).scalars()
        )

    assert retried is not None
    assert "Fix the intermittent validation failure." in retried.task_prompt
    assert "finish the remaining plan-conformance gaps" in retried.task_prompt
    assert "Do not restart from scratch" in retried.task_prompt
    assert "- Add regression test" in retried.task_prompt
    assert "- Wire retry endpoint" in retried.task_prompt

    assert len(operations) == 1
    evidence_ref = operations[0].payload["conformance_evidence_ref"]
    assert evidence_ref == {
        "source_workspace_id": first.id,
        "event_type": "workspace.state_changed",
        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
    }
    assert "Add regression test" not in str(operations[0].payload)
    assert operations[0].result["source_reason_code"] == PLAN_CONFORMANCE_UNSATISFIED
    assert retry_created[0].payload["source_reason_code"] == PLAN_CONFORMANCE_UNSATISFIED
    assert "conformance_evidence_ref" in retry_created[0].payload


@pytest.mark.unit
async def test_retry_conformance_unsatisfied_without_evidence_uses_original_prompt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    await _mark_conformance_failed_without_evidence(factory, first.id)

    retry = await service.retry_workspace(first.id)

    async with factory() as session:
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retry.new_workspace_id)
                )
            ).scalars()
        )
        retry_created = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == retry.new_workspace_id,
                        WorkspaceEvent.event_type == "workspace.retry_created",
                    )
                )
            ).scalars()
        )

    assert retried is not None
    assert retried.task_prompt == "Fix the intermittent validation failure."
    assert operations[0].payload == {"source_workspace_id": first.id}
    assert "source_reason_code" not in operations[0].result
    assert "source_reason_code" not in retry_created[0].payload
    assert "conformance_evidence_ref" not in retry_created[0].payload


@pytest.mark.unit
async def test_retry_planning_scope_violation_discards_premature_work_and_replans(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    await _mark_planning_scope_failed(factory, first.id)

    retry = await service.retry_workspace(first.id)

    async with factory() as session:
        original = await WorkspaceRepository(session).get(first.id)
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retry.new_workspace_id)
                )
            ).scalars()
        )
        retry_created = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == retry.new_workspace_id,
                        WorkspaceEvent.event_type == "workspace.retry_created",
                    )
                )
            ).scalars()
        )

    assert original is not None
    assert retried is not None
    assert original.branch_name == "awf/ws_scope_old"
    assert original.remote_push_branch == "awf/ws_scope_old"
    assert retried.branch_name is None
    assert retried.remote_push_branch is None
    assert retried.pr_url is None
    assert "Fix the intermittent validation failure." in retried.task_prompt
    assert "Discard the premature implementation from the failed planning attempt" in (
        retried.task_prompt
    )
    assert "Create or update only `docs/awf-plans/ws_scope_old.md`" in retried.task_prompt
    assert "src/awf/runtime/planning.py" in retried.task_prompt
    assert retried.task_policy.get("agent_model") is None

    assert len(operations) == 1
    operation_payload = operations[0].payload
    assert operation_payload is not None
    assert operation_payload["source_reason_code"] == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert operation_payload["planning_scope_evidence_ref"] == {
        "source_workspace_id": first.id,
        "event_type": "workspace.state_changed",
        "reason_code": AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    }
    assert operation_payload["recovery_strategy"] == "discard_and_replan"
    assert operation_payload["salvage_policy"] == "explicit_salvage_required"
    assert operation_payload["salvage"]["branch_name"] == "awf/ws_scope_old"
    assert "fallback_model" not in operation_payload
    assert operations[0].result["source_reason_code"] == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert operations[0].result["recovery_strategy"] == "discard_and_replan"
    assert retry_created[0].payload["source_reason_code"] == AGENT_PLAN_PHASE_SCOPE_VIOLATION
    assert retry_created[0].payload["salvage_policy"] == "explicit_salvage_required"


@pytest.mark.unit
async def test_retry_planning_scope_violation_applies_only_approved_fallback_model(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    await _mark_planning_scope_failed(
        factory,
        first.id,
        approved_fallback_model="gpt-5.5",
    )

    retry = await service.retry_workspace(first.id)

    async with factory() as session:
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retry.new_workspace_id)
                )
            ).scalars()
        )
        retry_created = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == retry.new_workspace_id,
                        WorkspaceEvent.event_type == "workspace.retry_created",
                    )
                )
            ).scalars()
        )

    assert retried is not None
    assert retried.task_policy["agent_model"] == "gpt-5.5"
    assert operations[0].payload["fallback_model"] == {
        "model": "gpt-5.5",
        "source": "task_policy.planning_scope_recovery.approved_fallback_model",
    }
    assert operations[0].result["fallback_model"]["model"] == "gpt-5.5"
    assert retry_created[0].payload["fallback_model"]["model"] == "gpt-5.5"


@pytest.mark.unit
async def test_retry_legacy_workspace_without_attempt_reuses_fallback_task(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.create(
            repo_url="git@github.com:example/retryable.git",
            branch_base="development",
            task_title="Retry legacy validation",
            task_prompt="Fix a legacy workspace without task attempts.",
            task_external_id=None,
            task_class="test_task",
            owned_paths=[],
            auto_merge=False,
            initial_review_grace_period_seconds=30,
            agent=AgentRuntime.codex.value,
            profile_ref="python",
            requested_profile={"source": "legacy-test-profile"},
            resolved_profile={"source": "legacy-test-profile"},
            test_commands=["uv run pytest tests/unit -q"],
        )
        await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="TEST")
        await repo.transition(source, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await session.commit()
        source_id = source.id

    service = WorkspaceService(factory)
    first_retry = await service.retry_workspace(source_id)
    second_retry = await service.retry_workspace(source_id)

    async with factory() as session:
        tasks = list((await session.execute(select(Task))).scalars())
        attempts = list(
            (
                await session.execute(
                    select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                )
            ).scalars()
        )

    assert len(tasks) == 1
    assert tasks[0].idempotency_key == f"retry-source-workspace:{source_id}"
    assert [attempt.workspace_id for attempt in attempts] == [
        first_retry.new_workspace_id,
        second_retry.new_workspace_id,
    ]
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert {attempt.task_id for attempt in attempts} == {tasks[0].id}


@pytest.mark.unit
async def test_retry_preserves_remote_push_branch_for_sync_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request(task_kind="sync_release_pr"))
    await _mark_failed(
        factory,
        first.id,
        branch_name="release-sync/ws_old",
        remote_push_branch="development",
    )

    retry = await service.retry_workspace(first.id)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        original = await repo.get(first.id)
        retried = await repo.get(retry.new_workspace_id)

    assert original is not None
    assert retried is not None
    assert original.task_kind == "sync_release_pr"
    assert original.branch_name == "release-sync/ws_old"
    assert original.remote_push_branch == "development"

    assert retried.task_kind == "sync_release_pr"
    assert retried.branch_name is None
    assert retried.remote_push_branch == "development"


@pytest.mark.unit
async def test_retry_persists_task_kind_without_post_insert_update() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = make_session_factory(engine)
    service = WorkspaceService(factory)
    first = await service.create_v2(_request(task_kind="sync_release_pr"))
    await _mark_failed(factory, first.id)

    statements: list[str] = []

    def record_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        await service.retry_workspace(first.id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
        await engine.dispose()

    task_kind_updates = [
        statement
        for statement in statements
        if statement.startswith("update workspaces") and "task_kind" in statement
    ]
    assert task_kind_updates == []
