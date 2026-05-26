"""Failure causality snapshot tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service import failure_causality as failure_causality_service
from awf.service.failure_causality import (
    build_preserved_failure_payload,
    load_failure_causality_snapshot,
    load_primary_failure_snapshot,
)


class _ScalarNoneResult:
    def scalar_one_or_none(self) -> None:
        return None


class _RecordingSession:
    def __init__(self, dialect_name: str) -> None:
        self.info = {SESSION_DIALECT_NAME_KEY: dialect_name}
        self.bind = None
        self.statements: list[Select[Any]] = []

    async def execute(self, statement: Select[Any]) -> _ScalarNoneResult:
        self.statements.append(statement)
        return _ScalarNoneResult()


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


async def _seed_failed_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    failure_reason: str,
    failure_message: str,
    reason_code: str,
    validation_reason_code: str,
    embedded_primary: dict[str, object] | None = None,
) -> tuple[str, str]:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure causality regression",
            task_prompt="Preserve the correct primary failure evidence.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        workspace.failure_reason = failure_reason
        workspace.failure_message = failure_message
        payload: dict[str, object] = {
            "reason_code": reason_code,
            "message": failure_message,
        }
        if embedded_primary is not None:
            payload["primary_failure"] = embedded_primary
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=reason_code,
            payload=payload,
        )

        validation_repo = ValidationRunRepository(session)
        validation_run = await validation_repo.start(
            workspace_id=workspace.id,
            attempt_id=None,
            tier=0,
            commands=[
                {
                    "command": "uv run pytest tests/unit/test_example.py::test_failure",
                    "phase": "validation",
                }
            ],
            base_commit="a" * 40,
            target_branch="main",
            target_head_sha="b" * 40,
            log_stream_refs={"validation": "logs/validation.log"},
            workspace_head_sha="c" * 40,
            profile_name="default",
            profile_version=1,
            profile_source=".awf/workspace.yml",
            resolved_profile_digest="d" * 64,
            environment_identity_digest="e" * 64,
            environment_identity_inputs={"python": "3.12"},
        )
        await validation_repo.finish(
            validation_run.id,
            status="failed",
            reason_code=validation_reason_code,
            coverage={
                "percent": 91.5,
                "minimum_percent": 99.0,
                "threshold": 99.0,
                "failing_test_node_ids": [
                    "tests/unit/test_example.py::test_failure",
                ],
                "failing_test_evidence": [
                    "FAILED tests/unit/test_example.py::test_failure",
                ],
            },
        )
        await session.commit()
        return workspace.id, validation_run.id


@pytest.mark.unit
async def test_primary_failure_snapshot_ignores_null_order_same_tick_reset_for_ordered_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    same_tick = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure causality null event order regression",
            task_prompt="Do not let unordered same-tick resets hide ordered failures.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")

        validation_repo = ValidationRunRepository(session)
        validation_run = await validation_repo.start(
            workspace_id=workspace.id,
            attempt_id=None,
            tier=0,
            commands=[
                {
                    "command": "uv run pytest tests/unit/test_example.py::test_failure",
                    "phase": "validation",
                }
            ],
            base_commit="a" * 40,
            target_branch="main",
            target_head_sha="b" * 40,
            log_stream_refs={"validation": "logs/validation.log"},
            workspace_head_sha="c" * 40,
            profile_name="default",
            profile_version=1,
            profile_source=".awf/workspace.yml",
            resolved_profile_digest="d" * 64,
            environment_identity_digest="e" * 64,
            environment_identity_inputs={"python": "3.12"},
            started_at=same_tick - timedelta(minutes=2),
        )
        await validation_repo.finish(
            validation_run.id,
            status="failed",
            reason_code="CURRENT_VALIDATION_FAILURE",
            finished_at=same_tick - timedelta(minutes=1),
        )

        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = "current validation failed before legacy reset"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="CURRENT_VALIDATION_FAILURE",
            payload={
                "reason_code": "CURRENT_VALIDATION_FAILURE",
                "message": "current validation failed before legacy reset",
            },
        )
        failed_event = next(
            event
            for event in workspace.events
            if event.new_state == WorkspaceStatus.failed.value
            and event.reason_code == "CURRENT_VALIDATION_FAILURE"
        )
        failed_event.occurred_at = same_tick
        assert failed_event.event_order is not None

        reset_event = await repo.add_event(
            workspace,
            event_type="workspace.remonitor_requested",
            reason_code="OPERATOR_REMONITOR",
            payload={
                "state_reset": {
                    "from": WorkspaceStatus.failed.value,
                    "to": WorkspaceStatus.monitoring_pr.value,
                },
            },
        )
        reset_event.occurred_at = same_tick
        reset_event.event_order = None
        workspace_id = workspace.id
        validation_run_id = validation_run.id
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.validation_failure.value
    assert snapshot["message"] == "current validation failed before legacy reset"
    assert snapshot["reason_code"] == "CURRENT_VALIDATION_FAILURE"
    assert snapshot["validation_run"]["id"] == validation_run_id


@pytest.mark.unit
async def test_remonitor_reset_event_order_precedes_same_tick_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    same_tick = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure causality remonitor event order regression",
            task_prompt="Order remonitor reset events against same-tick failures.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")

        workspace.status = WorkspaceStatus.monitoring_pr.value
        workspace.failure_reason = None
        workspace.failure_message = None
        reset_event = await repo.add_event(
            workspace,
            event_type="workspace.remonitor_requested",
            reason_code="OPERATOR_REMONITOR",
            payload={
                "state_reset": {
                    "from": WorkspaceStatus.failed.value,
                    "to": WorkspaceStatus.monitoring_pr.value,
                },
            },
        )
        reset_event.occurred_at = same_tick
        assert reset_event.event_order is not None
        reset_order = reset_event.event_order

        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "agent retry failed after ordered remonitor"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="AGENT_AUTH_FAILED",
            payload={
                "reason_code": "AGENT_AUTH_FAILED",
                "message": "agent retry failed after ordered remonitor",
            },
        )
        failed_event = next(
            event
            for event in workspace.events
            if event.new_state == WorkspaceStatus.failed.value
            and event.reason_code == "AGENT_AUTH_FAILED"
        )
        failed_event.occurred_at = same_tick
        assert failed_event.event_order is not None
        assert failed_event.event_order > reset_order
        workspace_id = workspace.id
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.agent_failure.value
    assert snapshot["message"] == "agent retry failed after ordered remonitor"
    assert snapshot["reason_code"] == "AGENT_AUTH_FAILED"
    assert "validation_run" not in snapshot


@pytest.mark.unit
async def test_primary_failure_snapshot_filters_validation_runs_before_current_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.validation_failure.value,
        failure_message="old pytest failure before remonitor",
        reason_code="OLD_PYTEST_FAILURE",
        validation_reason_code="OLD_PYTEST_FAILURE",
    )

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        validation_run = await ValidationRunRepository(session).get(validation_run_id)
        assert validation_run is not None
        old_failed_event = next(
            event
            for event in workspace.events
            if event.new_state == WorkspaceStatus.failed.value
            and event.reason_code == "OLD_PYTEST_FAILURE"
        )
        old_validation_finished_at = old_failed_event.occurred_at + timedelta(minutes=1)
        reset_at = old_validation_finished_at + timedelta(minutes=5)
        current_failure_at = reset_at + timedelta(minutes=5)
        old_failed_event.occurred_at = old_validation_finished_at
        validation_run.started_at = old_validation_finished_at - timedelta(minutes=1)
        validation_run.finished_at = old_validation_finished_at

        workspace.status = WorkspaceStatus.monitoring_pr.value
        workspace.failure_reason = None
        workspace.failure_message = None
        reset_event = await repo.add_event(
            workspace,
            event_type="workspace.state_changed",
            reason_code="OPERATOR_REMONITOR",
            payload={
                "state_reset": {
                    "from": WorkspaceStatus.failed.value,
                    "to": WorkspaceStatus.monitoring_pr.value,
                },
            },
        )
        reset_event.old_state = WorkspaceStatus.failed.value
        reset_event.new_state = WorkspaceStatus.monitoring_pr.value
        reset_event.occurred_at = reset_at

        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = "current validation failed before run finished"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="CURRENT_VALIDATION_FAILURE",
            payload={
                "reason_code": "CURRENT_VALIDATION_FAILURE",
                "message": "current validation failed before run finished",
            },
        )
        current_failure_event = next(
            event
            for event in workspace.events
            if event.new_state == WorkspaceStatus.failed.value
            and event.reason_code == "CURRENT_VALIDATION_FAILURE"
        )
        current_failure_event.occurred_at = current_failure_at
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.validation_failure.value
    assert snapshot["message"] == "current validation failed before run finished"
    assert snapshot["reason_code"] == "CURRENT_VALIDATION_FAILURE"
    assert "validation_run" not in snapshot
    assert "coverage" not in snapshot


@pytest.mark.unit
async def test_primary_failure_snapshot_ignores_non_state_active_event_after_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, _validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.infrastructure_failure.value,
        failure_message="cleanup failed after primary validation failure",
        reason_code="CLEANUP_FAILED",
        validation_reason_code="PYTEST_TEST_FAILURE",
        embedded_primary={
            "failure_reason": FailureReason.validation_failure.value,
            "message": "pytest failed before cleanup",
            "reason_code": "PYTEST_TEST_FAILURE",
            "details": {
                "operator_guidance": "Inspect the validation log before cleanup diagnostics."
            },
            "validation_run": {
                "id": "vr_embedded_primary_after_diagnostic",
                "status": "failed",
                "reason_code": "PYTEST_TEST_FAILURE",
            },
        },
    )

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.monitoring_pr.value
        await repo.add_event(
            workspace,
            event_type="workspace.diagnostic_note",
            reason_code="DIAGNOSTIC_NOTE",
            payload={"message": "operator requested extra monitoring diagnostics"},
        )
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.validation_failure.value
    assert snapshot["message"] == "pytest failed before cleanup"
    assert snapshot["reason_code"] == "PYTEST_TEST_FAILURE"
    assert snapshot["details"] == {
        "operator_guidance": "Inspect the validation log before cleanup diagnostics."
    }
    assert snapshot["validation_run"]["id"] == "vr_embedded_primary_after_diagnostic"


@pytest.mark.unit
async def test_primary_failure_snapshot_prefers_latest_failed_event_with_preserved_primary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, _validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.validation_failure.value,
        failure_message="pytest failed before cleanup",
        reason_code="PYTEST_TEST_FAILURE",
        validation_reason_code="PYTEST_TEST_FAILURE",
        embedded_primary={
            "failure_reason": FailureReason.validation_failure.value,
            "message": "pytest failed before cleanup",
            "reason_code": "PYTEST_TEST_FAILURE",
            "validation_run": {
                "id": "vr_preserved_primary",
                "status": "failed",
                "reason_code": "PYTEST_TEST_FAILURE",
            },
        },
    )

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None

        await repo.transition(
            workspace,
            to=WorkspaceStatus.destroying,
            reason_code="DESTROY_REQUESTED",
        )
        workspace.failure_reason = FailureReason.infrastructure_failure.value
        workspace.failure_message = "cleanup failed after primary validation failure"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="CLEANUP_FAILED",
            payload={
                "reason_code": "CLEANUP_FAILED",
                "message": "cleanup failed after primary validation failure",
            },
        )
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.validation_failure.value
    assert snapshot["message"] == "pytest failed before cleanup"
    assert snapshot["reason_code"] == "PYTEST_TEST_FAILURE"
    assert snapshot["validation_run"]["id"] == "vr_preserved_primary"


@pytest.mark.unit
async def test_failure_causality_snapshot_loads_current_epoch_secondary_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, _validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.validation_failure.value,
        failure_message="pytest failed before cleanup",
        reason_code="PYTEST_TEST_FAILURE",
        validation_reason_code="PYTEST_TEST_FAILURE",
    )
    cleanup_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "CLEANUP_FAILED",
    }
    stale_secondary = {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": "STALE_ACTIVE_EXECUTION",
    }

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        primary_snapshot = await load_primary_failure_snapshot(session, workspace)
        assert primary_snapshot is not None

        await repo.transition(
            workspace,
            to=WorkspaceStatus.destroying,
            reason_code="DESTROY_REQUESTED",
        )
        workspace.failure_reason = FailureReason.infrastructure_failure.value
        workspace.failure_message = "cleanup failed after primary validation failure"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="CLEANUP_FAILED",
            payload=build_preserved_failure_payload(
                primary_snapshot,
                secondary_failure=cleanup_secondary,
            ),
        )
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_failure_causality_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot.primary_failure["reason_code"] == "PYTEST_TEST_FAILURE"
    assert snapshot.secondary_failures == (cleanup_secondary,)

    payload = build_preserved_failure_payload(
        snapshot.primary_failure,
        secondary_failure=stale_secondary,
        previous_secondary_failures=snapshot.secondary_failures,
    )

    assert payload["secondary_failure"] == stale_secondary
    assert payload["secondary_failures"] == [cleanup_secondary, stale_secondary]


@pytest.mark.unit
async def test_failure_causality_snapshot_merges_secondary_history_from_latest_failed_event_without_embedded_primary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    primary = {
        "failure_reason": FailureReason.validation_failure.value,
        "message": "pytest failed before cleanup",
        "reason_code": "PYTEST_TEST_FAILURE",
        "validation_run": {
            "id": "vr_embedded_primary_before_mixed_event",
            "status": "failed",
            "reason_code": "PYTEST_TEST_FAILURE",
        },
    }
    cleanup_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "CLEANUP_FAILED",
    }
    stale_secondary = {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": "STALE_ACTIVE_EXECUTION",
    }

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure causality mixed event regression",
            task_prompt="Keep secondary history from newer failed events.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = "pytest failed before cleanup"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="CLEANUP_FAILED",
            payload=build_preserved_failure_payload(
                primary,
                secondary_failure=cleanup_secondary,
            ),
        )
        await repo.transition(
            workspace,
            to=WorkspaceStatus.destroying,
            reason_code="DESTROY_REQUESTED",
        )
        workspace.failure_reason = FailureReason.infrastructure_failure.value
        workspace.failure_message = "stale execution after cleanup"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="STALE_ACTIVE_EXECUTION",
            payload={
                "reason_code": "STALE_ACTIVE_EXECUTION",
                "message": "stale execution after cleanup",
                "secondary_failure": stale_secondary,
                "secondary_failures": [cleanup_secondary, stale_secondary],
            },
        )
        workspace_id = workspace.id
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_failure_causality_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot.primary_failure["reason_code"] == "PYTEST_TEST_FAILURE"
    assert snapshot.secondary_failures == (cleanup_secondary, stale_secondary)


@pytest.mark.unit
async def test_failure_causality_snapshot_reads_secondary_failure_recorded_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    primary_failure = {
        "failure_reason": FailureReason.validation_failure.value,
        "message": "pytest failed before cleanup",
        "reason_code": "PYTEST_TEST_FAILURE",
    }
    cleanup_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "CLEANUP_FAILED",
    }

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure causality secondary event regression",
            task_prompt="Read dedicated secondary failure events.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = "pytest failed before cleanup"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="PYTEST_TEST_FAILURE",
            payload={
                "reason_code": "PYTEST_TEST_FAILURE",
                "message": "pytest failed before cleanup",
            },
        )
        await repo.add_event(
            workspace,
            event_type="workspace.secondary_failure_recorded",
            reason_code="PYTEST_TEST_FAILURE",
            payload=build_preserved_failure_payload(
                primary_failure,
                secondary_failure=cleanup_secondary,
            ),
        )
        workspace_id = workspace.id
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_failure_causality_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot.primary_failure["reason_code"] == "PYTEST_TEST_FAILURE"
    assert snapshot.secondary_failures == (cleanup_secondary,)


@pytest.mark.unit
async def test_primary_failure_event_can_be_synthetic_secondary_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    primary_failure = {
        "failure_reason": FailureReason.validation_failure.value,
        "message": "pytest failed before cleanup",
        "reason_code": "PYTEST_TEST_FAILURE",
    }
    cleanup_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "CLEANUP_FAILED",
    }

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure causality synthetic primary event source",
            task_prompt="Document secondary failure events as primary evidence carriers.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = "pytest failed before cleanup"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="PYTEST_TEST_FAILURE",
            payload={
                "reason_code": "PYTEST_TEST_FAILURE",
                "message": "pytest failed before cleanup",
            },
        )
        synthetic_event = await repo.add_event(
            workspace,
            event_type="workspace.secondary_failure_recorded",
            reason_code="PYTEST_TEST_FAILURE",
            payload=build_preserved_failure_payload(
                primary_failure,
                secondary_failure=cleanup_secondary,
            ),
        )
        workspace_id = workspace.id
        synthetic_event_id = synthetic_event.id
        await session.commit()

    async with session_factory() as session:
        primary_event = await failure_causality_service._primary_failure_event_for_current_epoch(
            session,
            workspace_id,
        )

    assert primary_event is not None
    assert primary_event.id == synthetic_event_id
    assert primary_event.event_type == "workspace.secondary_failure_recorded"


@pytest.mark.unit
async def test_failure_causality_snapshot_dedupes_truncated_secondary_history_windows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    primary_at = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)
    truncated_at = datetime(2100, 1, 1, 12, 1, tzinfo=UTC)
    latest_at = datetime(2100, 1, 1, 12, 2, tzinfo=UTC)
    primary_failure = {
        "failure_reason": FailureReason.validation_failure.value,
        "message": "pytest failed before cleanup",
        "reason_code": "PYTEST_TEST_FAILURE",
    }
    initial_history = [
        {"failure_reason": "cleanup_failure", "reason_code": f"CLEANUP_FAILED_{index}"}
        for index in range(4)
    ]
    truncated_history = initial_history[1:3]
    latest_secondary = {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": "STALE_ACTIVE_EXECUTION",
    }

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure causality truncated secondary regression",
            task_prompt="Do not double-count re-extracted truncated secondary histories.",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.failed.value
        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = "pytest failed before cleanup"
        session.add_all(
            [
                WorkspaceEvent(
                    id="evt_primary_with_secondary_history",
                    workspace_id=workspace.id,
                    event_type="workspace.state_changed",
                    old_state=WorkspaceStatus.running.value,
                    new_state=WorkspaceStatus.failed.value,
                    reason_code="PYTEST_TEST_FAILURE",
                    payload={
                        "primary_failure": primary_failure,
                        "secondary_failures": initial_history,
                    },
                    occurred_at=primary_at,
                    event_order=1,
                ),
                WorkspaceEvent(
                    id="evt_truncated_secondary_history",
                    workspace_id=workspace.id,
                    event_type="workspace.secondary_failure_recorded",
                    old_state=WorkspaceStatus.failed.value,
                    new_state=WorkspaceStatus.failed.value,
                    reason_code="CLEANUP_HISTORY_TRUNCATED",
                    payload={"secondary_failures": truncated_history},
                    occurred_at=truncated_at,
                    event_order=2,
                ),
                WorkspaceEvent(
                    id="evt_latest_secondary_history",
                    workspace_id=workspace.id,
                    event_type="workspace.secondary_failure_recorded",
                    old_state=WorkspaceStatus.failed.value,
                    new_state=WorkspaceStatus.failed.value,
                    reason_code="STALE_ACTIVE_EXECUTION",
                    payload={
                        "secondary_failures": [*truncated_history, latest_secondary],
                    },
                    occurred_at=latest_at,
                    event_order=3,
                ),
            ]
        )
        workspace_id = workspace.id
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_failure_causality_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot.primary_failure["reason_code"] == "PYTEST_TEST_FAILURE"
    assert snapshot.secondary_failures == (*initial_history, latest_secondary)


@pytest.mark.unit
async def test_failure_causality_snapshot_orders_same_timestamp_secondary_history_null_event_orders_last(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    primary_at = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)
    secondary_at = datetime(2100, 1, 1, 12, 1, tzinfo=UTC)
    latest_at = datetime(2100, 1, 1, 12, 2, tzinfo=UTC)
    primary_failure = {
        "failure_reason": FailureReason.validation_failure.value,
        "message": "pytest failed before cleanup",
        "reason_code": "PYTEST_TEST_FAILURE",
    }
    ordered_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "ORDERED_CLEANUP_FAILED",
    }
    legacy_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "LEGACY_CLEANUP_FAILED",
    }

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure causality null order regression",
            task_prompt="Keep legacy null-order secondary history after ordered rows.",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.failed.value
        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = "pytest failed before cleanup"
        session.add_all(
            [
                WorkspaceEvent(
                    id="evt_primary_failure",
                    workspace_id=workspace.id,
                    event_type="workspace.state_changed",
                    old_state=WorkspaceStatus.running.value,
                    new_state=WorkspaceStatus.failed.value,
                    reason_code="PYTEST_TEST_FAILURE",
                    payload={
                        "reason_code": "PYTEST_TEST_FAILURE",
                        "message": "pytest failed before cleanup",
                        "primary_failure": primary_failure,
                    },
                    occurred_at=primary_at,
                    event_order=1,
                ),
                WorkspaceEvent(
                    id="evt_ordered_secondary",
                    workspace_id=workspace.id,
                    event_type="workspace.secondary_failure_recorded",
                    old_state=WorkspaceStatus.failed.value,
                    new_state=WorkspaceStatus.failed.value,
                    reason_code="ORDERED_CLEANUP_FAILED",
                    payload={
                        "secondary_failure": ordered_secondary,
                        "secondary_failures": [ordered_secondary],
                    },
                    occurred_at=secondary_at,
                    event_order=2,
                ),
                WorkspaceEvent(
                    id="evt_legacy_secondary",
                    workspace_id=workspace.id,
                    event_type="workspace.secondary_failure_recorded",
                    old_state=WorkspaceStatus.failed.value,
                    new_state=WorkspaceStatus.failed.value,
                    reason_code="LEGACY_CLEANUP_FAILED",
                    payload={
                        "secondary_failure": legacy_secondary,
                        "secondary_failures": [legacy_secondary],
                    },
                    occurred_at=secondary_at,
                    event_order=None,
                ),
                WorkspaceEvent(
                    id="evt_latest_failed",
                    workspace_id=workspace.id,
                    event_type="workspace.state_changed",
                    old_state=WorkspaceStatus.failed.value,
                    new_state=WorkspaceStatus.failed.value,
                    reason_code="LATEST_FAILURE",
                    payload={"reason_code": "LATEST_FAILURE"},
                    occurred_at=latest_at,
                    event_order=3,
                ),
            ]
        )
        workspace_id = workspace.id
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_failure_causality_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot.primary_failure["reason_code"] == "PYTEST_TEST_FAILURE"
    assert snapshot.secondary_failures == (ordered_secondary, legacy_secondary)


@pytest.mark.unit
async def test_failure_causality_snapshot_orders_same_timestamp_failures_by_event_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    same_tick = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)
    old_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "OLD_CLEANUP_FAILED",
    }
    current_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "CURRENT_CLEANUP_FAILED",
    }
    old_primary = {
        "failure_reason": FailureReason.validation_failure.value,
        "message": "old validation failed before cleanup",
        "reason_code": "OLD_VALIDATION_FAILURE",
        "validation_run": {
            "id": "vr_old_same_tick",
            "status": "failed",
            "reason_code": "OLD_VALIDATION_FAILURE",
        },
    }
    current_primary = {
        "failure_reason": FailureReason.validation_failure.value,
        "message": "current validation failed before cleanup",
        "reason_code": "CURRENT_VALIDATION_FAILURE",
        "validation_run": {
            "id": "vr_current_same_tick",
            "status": "failed",
            "reason_code": "CURRENT_VALIDATION_FAILURE",
        },
    }

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure causality event-order regression",
            task_prompt="Do not order same-timestamp failure events by random IDs.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        workspace.failure_reason = FailureReason.infrastructure_failure.value
        workspace.failure_message = "old cleanup failed after validation"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="OLD_CLEANUP_FAILED",
            payload=build_preserved_failure_payload(
                old_primary,
                secondary_failure=old_secondary,
            ),
        )
        old_failed_event = next(
            event
            for event in workspace.events
            if event.new_state == WorkspaceStatus.failed.value
            and event.reason_code == "OLD_CLEANUP_FAILED"
        )

        await repo.transition(
            workspace,
            to=WorkspaceStatus.destroying,
            reason_code="DESTROY_REQUESTED",
        )
        workspace.failure_reason = FailureReason.infrastructure_failure.value
        workspace.failure_message = "current cleanup failed after validation"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="CURRENT_CLEANUP_FAILED",
            payload=build_preserved_failure_payload(
                current_primary,
                secondary_failure=current_secondary,
            ),
        )
        current_failed_event = next(
            event
            for event in workspace.events
            if event.new_state == WorkspaceStatus.failed.value
            and event.reason_code == "CURRENT_CLEANUP_FAILED"
        )

        old_failed_event.id = "evt_zzzzzzzzzzzzzzzzzzzzzzzz"
        current_failed_event.id = "evt_aaaaaaaaaaaaaaaaaaaaaaaa"
        old_failed_event.occurred_at = same_tick
        current_failed_event.occurred_at = same_tick
        assert current_failed_event.id < old_failed_event.id
        assert old_failed_event.event_order is not None
        assert current_failed_event.event_order is not None
        assert current_failed_event.event_order > old_failed_event.event_order
        workspace_id = workspace.id
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_failure_causality_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot.primary_failure["reason_code"] == "CURRENT_VALIDATION_FAILURE"
    assert snapshot.primary_failure["validation_run"]["id"] == "vr_current_same_tick"
    assert snapshot.secondary_failures == (current_secondary,)


@pytest.mark.unit
async def test_failure_causality_snapshot_prefers_later_same_tick_state_failure_over_secondary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    same_tick = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)
    original_primary = {
        "failure_reason": FailureReason.validation_failure.value,
        "message": "pytest failed before cleanup",
        "reason_code": "PYTEST_TEST_FAILURE",
    }
    cleanup_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "CLEANUP_FAILED",
    }
    terminal_primary = {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "message": "terminal release failed after cleanup",
        "reason_code": "TERMINAL_RELEASE_FAILED",
    }
    terminal_secondary = {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": "TERMINAL_RUNTIME_RELEASE_FAILED",
    }

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure causality same-tick synthetic ordering regression",
            task_prompt="A later real failed transition wins over a same-tick synthetic event.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(workspace, to=WorkspaceStatus.running, reason_code="SEED")
        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = "pytest failed before cleanup"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="PYTEST_TEST_FAILURE",
            payload={"primary_failure": original_primary},
        )
        secondary_event = await repo.add_event(
            workspace,
            event_type="workspace.secondary_failure_recorded",
            reason_code="PYTEST_TEST_FAILURE",
            payload=build_preserved_failure_payload(
                original_primary,
                secondary_failure=cleanup_secondary,
            ),
        )
        await repo.transition(
            workspace,
            to=WorkspaceStatus.destroying,
            reason_code="DESTROY_REQUESTED",
        )
        workspace.failure_reason = FailureReason.infrastructure_failure.value
        workspace.failure_message = "terminal release failed after cleanup"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="TERMINAL_RELEASE_FAILED",
            payload=build_preserved_failure_payload(
                terminal_primary,
                secondary_failure=terminal_secondary,
            ),
        )
        latest_failed_event = next(
            event
            for event in workspace.events
            if event.new_state == WorkspaceStatus.failed.value
            and event.reason_code == "TERMINAL_RELEASE_FAILED"
        )
        secondary_event.occurred_at = same_tick
        latest_failed_event.occurred_at = same_tick
        assert secondary_event.event_order is not None
        assert latest_failed_event.event_order is not None
        assert latest_failed_event.event_order > secondary_event.event_order
        workspace_id = workspace.id
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_failure_causality_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot.primary_failure["reason_code"] == "TERMINAL_RELEASE_FAILED"
    assert snapshot.secondary_failures == (terminal_secondary,)
