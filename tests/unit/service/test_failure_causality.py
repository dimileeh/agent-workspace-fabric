"""Failure causality snapshot tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.failure_causality import load_primary_failure_snapshot


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


@pytest.mark.unit
async def test_primary_failure_snapshot_omits_historical_validation_run_for_agent_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.agent_failure.value,
        failure_message="provider auth failed before runtime cleanup",
        reason_code="AGENT_AUTH_FAILED",
        validation_reason_code="PYTEST_TEST_FAILURE",
    )

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.agent_failure.value
    assert snapshot["message"] == "provider auth failed before runtime cleanup"
    assert snapshot["reason_code"] == "AGENT_AUTH_FAILED"
    assert "validation_run" not in snapshot
    assert "coverage" not in snapshot
    assert "coverage_percent" not in snapshot
    assert validation_run_id


@pytest.mark.unit
async def test_primary_failure_snapshot_keeps_validation_run_for_validation_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.validation_failure.value,
        failure_message="pytest failed before runtime cleanup",
        reason_code="PYTEST_TEST_FAILURE",
        validation_reason_code="PYTEST_TEST_FAILURE",
    )

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.validation_failure.value
    assert snapshot["reason_code"] == "PYTEST_TEST_FAILURE"
    assert snapshot["validation_run"]["id"] == validation_run_id
    assert snapshot["coverage"]["failing_test_node_ids"] == [
        "tests/unit/test_example.py::test_failure"
    ]
    assert snapshot["coverage_percent"] == 91.5
    assert snapshot["coverage_minimum_percent"] == 99.0


@pytest.mark.unit
async def test_primary_failure_snapshot_preserves_embedded_validation_payload_for_agent_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    embedded_validation_run_id = "vr_embedded"
    workspace_id, validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.agent_failure.value,
        failure_message="provider auth failed before runtime cleanup",
        reason_code="AGENT_AUTH_FAILED",
        validation_reason_code="UNRELATED_VALIDATION_FAILURE",
        embedded_primary={
            "failure_reason": FailureReason.agent_failure.value,
            "message": "provider auth failed before runtime cleanup",
            "reason_code": "AGENT_AUTH_FAILED",
            "validation_run": {
                "id": embedded_validation_run_id,
                "status": "failed",
                "reason_code": "EMBEDDED_VALIDATION_FAILURE",
            },
            "coverage": {"percent": 88.0, "threshold": 99.0},
        },
    )

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["reason_code"] == "AGENT_AUTH_FAILED"
    assert snapshot["validation_run"]["id"] == embedded_validation_run_id
    assert snapshot["validation_run"]["reason_code"] == "EMBEDDED_VALIDATION_FAILURE"
    assert snapshot["coverage"] == {"percent": 88.0, "threshold": 99.0}
    assert snapshot["validation_run"]["id"] != validation_run_id


@pytest.mark.unit
async def test_primary_failure_snapshot_preserves_embedded_validation_payload_for_validation_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    embedded_validation_run_id = "vr_embedded_validation_failure"
    workspace_id, validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.validation_failure.value,
        failure_message="pytest failed before runtime cleanup",
        reason_code="PYTEST_TEST_FAILURE",
        validation_reason_code="LATER_UNRELATED_VALIDATION_FAILURE",
        embedded_primary={
            "failure_reason": FailureReason.validation_failure.value,
            "message": "pytest failed before runtime cleanup",
            "reason_code": "EMBEDDED_PYTEST_FAILURE",
            "validation_run": {
                "id": embedded_validation_run_id,
                "status": "failed",
                "reason_code": "EMBEDDED_PYTEST_FAILURE",
            },
            "coverage": {"percent": 87.0, "threshold": 99.0},
        },
    )

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["reason_code"] == "EMBEDDED_PYTEST_FAILURE"
    assert snapshot["validation_run"]["id"] == embedded_validation_run_id
    assert snapshot["validation_run"]["reason_code"] == "EMBEDDED_PYTEST_FAILURE"
    assert snapshot["coverage"] == {"percent": 87.0, "threshold": 99.0}
    assert snapshot["validation_run"]["id"] != validation_run_id


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
