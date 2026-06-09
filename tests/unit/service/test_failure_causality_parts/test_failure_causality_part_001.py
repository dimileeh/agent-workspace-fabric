"""Failure causality snapshot tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service import failure_causality as failure_causality_service
from awf.service.failure_causality import (
    attach_primary_failure,
    build_preserved_failure_payload,
    load_primary_failure_snapshot,
    restore_primary_failure_row_fields,
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
def test_preserved_failure_payload_keeps_latest_secondary_and_history() -> None:
    secondary_failure = {
        "failure_reason": "cleanup_failure",
        "reason_code": "CLEANUP_FAILED",
    }
    payload = build_preserved_failure_payload(
        {
            "failure_reason": FailureReason.validation_failure.value,
            "reason_code": "PYTEST_TEST_FAILURE",
            "message": "pytest failed",
        },
        secondary_failure=secondary_failure,
    )

    assert payload["reason_code"] == "PYTEST_TEST_FAILURE"
    assert payload["secondary_failure"] == secondary_failure
    assert payload["secondary_failures"] == [secondary_failure]


@pytest.mark.unit
def test_restore_primary_failure_row_fields_preserves_bounded_primary_message() -> None:
    workspace = Workspace(
        id="ws_restore_primary",
        status=WorkspaceStatus.failed.value,
        repo_url="git@github.com:example/app.git",
        branch_base="main",
        task_title="Restore primary failure",
        task_prompt="Preserve primary failure row fields.",
        agent="codex",
    )
    message = "validation failed: " + ("x" * 4096)

    restore_primary_failure_row_fields(
        workspace,
        {
            "failure_reason": FailureReason.validation_failure.value,
            "message": message,
        },
    )

    assert workspace.failure_reason == FailureReason.validation_failure.value
    assert workspace.failure_message == message[:2048]


@pytest.mark.unit
def test_restore_primary_failure_row_fields_clears_missing_failure_reason() -> None:
    workspace = Workspace(
        id="ws_restore_primary_missing_reason",
        status=WorkspaceStatus.failed.value,
        repo_url="git@github.com:example/app.git",
        branch_base="main",
        task_title="Restore primary failure",
        task_prompt="Preserve primary failure row fields.",
        agent="codex",
    )
    workspace.failure_reason = FailureReason.infrastructure_failure.value
    workspace.failure_message = "secondary failure"

    restore_primary_failure_row_fields(
        workspace,
        {
            "message": "primary failed before secondary failure",
            "reason_code": "PRIMARY_FAILED",
        },
    )

    assert workspace.failure_reason is None
    assert workspace.failure_message == "primary failed before secondary failure"


@pytest.mark.unit
def test_restore_primary_failure_row_fields_clears_missing_failure_message() -> None:
    workspace = Workspace(
        id="ws_restore_primary_missing_message",
        status=WorkspaceStatus.failed.value,
        repo_url="git@github.com:example/app.git",
        branch_base="main",
        task_title="Restore primary failure",
        task_prompt="Clear stale secondary failure row fields.",
        agent="codex",
    )
    workspace.failure_reason = FailureReason.infrastructure_failure.value
    workspace.failure_message = "secondary failure"

    restore_primary_failure_row_fields(
        workspace,
        {
            "failure_reason": FailureReason.validation_failure.value,
            "reason_code": "PRIMARY_FAILED",
        },
    )

    assert workspace.failure_reason == FailureReason.validation_failure.value
    assert workspace.failure_message is None


@pytest.mark.unit
async def test_latest_failed_state_event_uses_sqlite_json_type_for_primary_filter() -> None:
    session = _RecordingSession("sqlite")

    await failure_causality_service._latest_failed_state_event(
        session,  # type: ignore[arg-type]
        "ws_sqlite_primary_filter",
        require_primary_failure=True,
    )

    sql = str(session.statements[0].compile(dialect=sqlite.dialect()))
    assert "json_typeof" not in sql
    assert "json_type" in sql


@pytest.mark.unit
async def test_failure_epoch_reset_detection_uses_sqlite_json_type_for_remonitor_reset() -> None:
    session = _RecordingSession("sqlite")
    failed_event = WorkspaceEvent(
        id="evt_sqlite_failed_event",
        workspace_id="ws_sqlite_reset_filter",
        event_type="workspace.state_changed",
        old_state=WorkspaceStatus.running.value,
        new_state=WorkspaceStatus.failed.value,
        reason_code="PYTEST_TEST_FAILURE",
        payload={"reason_code": "PYTEST_TEST_FAILURE"},
        occurred_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        event_order=1,
    )

    await failure_causality_service._has_failure_epoch_reset_after(
        session,  # type: ignore[arg-type]
        "ws_sqlite_reset_filter",
        failed_event,
    )

    sql = str(session.statements[0].compile(dialect=sqlite.dialect()))
    assert "json_typeof" not in sql
    assert "json_type" in sql


@pytest.mark.unit
async def test_failure_causality_json_object_filters_keep_postgresql_json_typeof() -> None:
    session = _RecordingSession("postgresql")

    await failure_causality_service._latest_failed_state_event(
        session,  # type: ignore[arg-type]
        "ws_postgresql_primary_filter",
        require_primary_failure=True,
    )

    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "json_typeof" in sql


@pytest.mark.unit
def test_failure_causality_json_object_filter_unknown_dialect_is_false() -> None:
    session = _RecordingSession("mysql")

    predicate = failure_causality_service._json_payload_object_predicate(  # noqa: SLF001
        session,  # type: ignore[arg-type]
        "primary_failure",
    )

    assert str(predicate.compile(dialect=sqlite.dialect())) == "0"


@pytest.mark.unit
def test_failure_causality_session_dialect_falls_back_to_bind_name() -> None:
    class _Dialect:
        name = "sqlite"

    class _Bind:
        dialect = _Dialect()

    class _Session:
        info: dict[str, object] = {}
        bind = _Bind()

    assert (
        failure_causality_service._session_dialect_name(  # noqa: SLF001
            _Session()  # type: ignore[arg-type]
        )
        == "sqlite"
    )


@pytest.mark.unit
def test_failure_causality_session_dialect_returns_none_without_string_name() -> None:
    class _Session:
        info: dict[str, object] = {}
        bind = object()

    assert (
        failure_causality_service._session_dialect_name(  # noqa: SLF001
            _Session()  # type: ignore[arg-type]
        )
        is None
    )


@pytest.mark.unit
def test_failure_causality_session_dialect_returns_none_without_bind() -> None:
    class _Session:
        info: dict[str, object] = {}
        bind = None

    assert (
        failure_causality_service._session_dialect_name(  # noqa: SLF001
            _Session()  # type: ignore[arg-type]
        )
        is None
    )


@pytest.mark.unit
def test_primary_failure_reason_code_prefers_primary_reason() -> None:
    assert (
        failure_causality_service.primary_failure_reason_code(
            {"reason_code": "PYTEST_TEST_FAILURE"},
            fallback="FALLBACK",
        )
        == "PYTEST_TEST_FAILURE"
    )


@pytest.mark.unit
def test_primary_failure_reason_code_uses_fallback_for_blank_primary_reason() -> None:
    assert (
        failure_causality_service.primary_failure_reason_code(
            {},
            fallback="FALLBACK",
        )
        == "FALLBACK"
    )


@pytest.mark.unit
def test_preserved_failure_payload_allows_primary_without_reason_or_message() -> None:
    payload = build_preserved_failure_payload(
        {"details": {"exit_code": 1}},
        secondary_failure={"reason_code": "CLEANUP_FAILED"},
    )

    assert "reason_code" not in payload
    assert "message" not in payload
    assert payload["details"] == {"exit_code": 1}
    assert payload["secondary_failure"] == {"reason_code": "CLEANUP_FAILED"}


@pytest.mark.unit
def test_secondary_failure_history_prefix_accepts_empty_prefix() -> None:
    assert failure_causality_service._secondary_failure_history_contains(  # noqa: SLF001
        [{"reason_code": "CLEANUP_FAILED"}],
        [],
    )


@pytest.mark.unit
def test_preserved_failure_payload_accumulates_prior_secondary_failures() -> None:
    prior_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "CLEANUP_FAILED",
    }
    current_secondary = {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": "STALE_ACTIVE_EXECUTION",
    }

    payload = build_preserved_failure_payload(
        {
            "failure_reason": FailureReason.validation_failure.value,
            "reason_code": "PYTEST_TEST_FAILURE",
            "message": "pytest failed",
        },
        secondary_failure=current_secondary,
        previous_secondary_failures=(prior_secondary,),
    )

    assert payload["secondary_failure"] == current_secondary
    assert payload["secondary_failures"] == [prior_secondary, current_secondary]


@pytest.mark.unit
def test_failure_causality_small_helpers_cover_missing_and_fallback_shapes() -> None:
    assert (
        failure_causality_service.primary_failure_reason_code(
            {"reason_code": 123},
            fallback="FALLBACK_REASON",
        )
        == "FALLBACK_REASON"
    )
    assert (
        failure_causality_service.primary_failure_reason_code(
            None,
            fallback="FALLBACK_REASON",
        )
        == "FALLBACK_REASON"
    )

    payload = build_preserved_failure_payload(
        {
            "failure_reason": FailureReason.validation_failure.value,
            "details": {"validation_run_id": "vr_123"},
        },
        secondary_failure={"reason_code": "CLEANUP_FAILED"},
    )
    assert payload["details"] == {"validation_run_id": "vr_123"}
    assert "reason_code" not in payload
    assert "message" not in payload

    sqlite_fallback = failure_causality_service._json_payload_object_predicate(  # noqa: SLF001
        _RecordingSession("mysql"),  # type: ignore[arg-type]
        "primary_failure",
    )
    assert str(sqlite_fallback.compile(dialect=postgresql.dialect())) == "false"

    class _NoBindSession:
        info: dict[str, object] = {}
        bind = None

    class _NamelessDialect:
        name = 123

    class _NamelessBind:
        dialect = _NamelessDialect()

    class _NamelessBindSession:
        info: dict[str, object] = {}
        bind = _NamelessBind()

    assert failure_causality_service._session_dialect_name(_NoBindSession()) is None  # type: ignore[arg-type]  # noqa: SLF001
    assert failure_causality_service._session_dialect_name(_NamelessBindSession()) is None  # type: ignore[arg-type]  # noqa: SLF001
    assert failure_causality_service._secondary_failure_history_contains([], []) is True  # noqa: SLF001


@pytest.mark.unit
def test_preserved_failure_payload_caps_secondary_failure_history() -> None:
    expected_limit = 20
    previous_secondaries = tuple(
        {
            "failure_reason": "cleanup_failure",
            "reason_code": f"CLEANUP_FAILED_{index}",
        }
        for index in range(expected_limit + 3)
    )
    current_secondary = {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": "STALE_ACTIVE_EXECUTION",
    }

    payload = build_preserved_failure_payload(
        {
            "failure_reason": FailureReason.validation_failure.value,
            "reason_code": "PYTEST_TEST_FAILURE",
            "message": "pytest failed",
        },
        secondary_failure=current_secondary,
        previous_secondary_failures=previous_secondaries,
    )

    retained = payload["secondary_failures"]
    expected_prior_start = len(previous_secondaries) - (expected_limit - 1)
    assert payload["secondary_failure"] == current_secondary
    assert len(retained) == expected_limit
    assert retained[:-1] == list(previous_secondaries[expected_prior_start:])
    assert retained[-1] == current_secondary


@pytest.mark.unit
def test_secondary_failure_history_reader_returns_bounded_tail() -> None:
    expected_limit = 20
    previous_secondaries = [
        {
            "failure_reason": "cleanup_failure",
            "reason_code": f"CLEANUP_FAILED_{index}",
        }
        for index in range(expected_limit + 3)
    ]
    legacy_secondary = {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": "STALE_ACTIVE_EXECUTION",
    }

    history = failure_causality_service._secondary_failure_history(
        {
            "secondary_failures": previous_secondaries,
            "secondary_failure": legacy_secondary,
        }
    )

    expected_prior_start = len(previous_secondaries) + 1 - expected_limit
    assert len(history) == expected_limit
    assert history[:-1] == tuple(previous_secondaries[expected_prior_start:])
    assert history[-1] == legacy_secondary


@pytest.mark.unit
def test_preserved_failure_payload_ignores_secondary_history_in_extra_payload() -> None:
    ignored_secondary = {
        "failure_reason": "cleanup_failure",
        "reason_code": "CLEANUP_FAILED",
    }
    prior_secondary = {
        "failure_reason": "runtime_stranding",
        "reason_code": "RUNTIME_STRANDED",
    }
    current_secondary = {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": "STALE_ACTIVE_EXECUTION",
    }

    payload = build_preserved_failure_payload(
        {
            "failure_reason": FailureReason.validation_failure.value,
            "reason_code": "PYTEST_TEST_FAILURE",
            "message": "pytest failed",
        },
        secondary_failure=current_secondary,
        extra={"secondary_failure": ignored_secondary},
        previous_secondary_failures=(prior_secondary,),
    )

    assert payload["secondary_failure"] == current_secondary
    assert payload["secondary_failures"] == [prior_secondary, current_secondary]


@pytest.mark.unit
def test_attach_primary_failure_preserves_existing_primary_failure_key() -> None:
    existing_primary = {
        "failure_reason": FailureReason.validation_failure.value,
        "reason_code": "PYTEST_TEST_FAILURE",
    }
    loaded_primary = {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": "STALE_ACTIVE_EXECUTION",
    }

    payload = attach_primary_failure(
        {"primary_failure": existing_primary, "message": "keep existing evidence"},
        loaded_primary,
    )

    assert payload["primary_failure"] == existing_primary
    assert payload["message"] == "keep existing evidence"


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
async def test_primary_failure_snapshot_does_not_tiebreak_validation_runs_by_random_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, older_validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.validation_failure.value,
        failure_message="pytest failed with same-tick validation callbacks",
        reason_code="OLD_PYTEST_FAILURE",
        validation_reason_code="OLD_PYTEST_FAILURE",
    )

    async with session_factory() as session:
        validation_repo = ValidationRunRepository(session)
        older_run = await validation_repo.get(older_validation_run_id)
        assert older_run is not None
        same_started_at = older_run.started_at + timedelta(seconds=1)
        same_finished_at = same_started_at + timedelta(seconds=1)
        older_run.id = "vr_zzzzzzzzzzzzzzzzzzzzzzzz"
        older_run.reason_code = "OLD_PYTEST_FAILURE"
        older_run.started_at = same_started_at
        older_run.finished_at = same_finished_at
        older_run.created_at = same_started_at
        older_run.updated_at = same_finished_at
        older_run.coverage = {
            "percent": 80.0,
            "minimum_percent": 99.0,
            "failing_test_node_ids": ["tests/unit/test_example.py::test_old_failure"],
        }

        newer_run = await validation_repo.start(
            workspace_id=workspace_id,
            attempt_id=None,
            tier=0,
            commands=[
                {
                    "command": "uv run pytest tests/unit/test_example.py::test_current_failure",
                    "phase": "validation",
                }
            ],
            base_commit="a" * 40,
            target_branch="main",
            target_head_sha="b" * 40,
            log_stream_refs={"validation": "logs/current-validation.log"},
            workspace_head_sha="c" * 40,
            profile_name="default",
            profile_version=1,
            profile_source=".awf/workspace.yml",
            resolved_profile_digest="d" * 64,
            environment_identity_digest="e" * 64,
            environment_identity_inputs={"python": "3.12"},
            started_at=same_started_at,
        )
        await validation_repo.finish(
            newer_run.id,
            status="failed",
            reason_code="CURRENT_PYTEST_FAILURE",
            finished_at=same_finished_at,
            coverage={
                "percent": 91.5,
                "minimum_percent": 99.0,
                "failing_test_node_ids": ["tests/unit/test_example.py::test_current_failure"],
            },
        )
        newer_run.id = "vr_aaaaaaaaaaaaaaaaaaaaaaaa"
        newer_run.created_at = same_started_at + timedelta(microseconds=1)
        newer_run.updated_at = same_finished_at
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["reason_code"] == "CURRENT_PYTEST_FAILURE"
    assert snapshot["validation_run"]["id"] == "vr_aaaaaaaaaaaaaaaaaaaaaaaa"
    assert snapshot["coverage"]["failing_test_node_ids"] == [
        "tests/unit/test_example.py::test_current_failure"
    ]


@pytest.mark.unit
async def test_primary_failure_snapshot_ignores_later_stale_validation_callback_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.validation_failure.value,
        failure_message="pytest failed before stale callback",
        reason_code="PYTEST_TEST_FAILURE",
        validation_reason_code="PYTEST_TEST_FAILURE",
    )

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.record_ignored_stale_callback(
            workspace,
            callback_source="executor",
            callback_action="validate",
            expected_status=WorkspaceStatus.validating,
            reason_code="STALE_CALLBACK_IGNORED",
        )
        validation_repo = ValidationRunRepository(session)
        stale_started_at = datetime.now(UTC) + timedelta(minutes=5)
        stale_run = await validation_repo.start(
            workspace_id=workspace.id,
            attempt_id=None,
            tier=0,
            commands=[
                {
                    "command": "uv run pytest tests/unit/test_example.py::test_stale_callback",
                    "phase": "validation",
                }
            ],
            base_commit="a" * 40,
            target_branch="main",
            target_head_sha="b" * 40,
            log_stream_refs={"validation": "logs/stale-validation.log"},
            workspace_head_sha="c" * 40,
            profile_name="default",
            profile_version=1,
            profile_source=".awf/workspace.yml",
            resolved_profile_digest="d" * 64,
            environment_identity_digest="e" * 64,
            environment_identity_inputs={"python": "3.12"},
            started_at=stale_started_at,
        )
        await validation_repo.finish(
            stale_run.id,
            status="failed",
            reason_code="STALE_CALLBACK_IGNORED",
            finished_at=stale_started_at + timedelta(seconds=1),
        )
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["reason_code"] == "PYTEST_TEST_FAILURE"
    assert snapshot["validation_run"]["id"] == validation_run_id
    assert snapshot["validation_run"]["reason_code"] == "PYTEST_TEST_FAILURE"
    assert snapshot["coverage"]["failing_test_node_ids"] == [
        "tests/unit/test_example.py::test_failure"
    ]


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


@pytest.mark.unit
async def test_primary_failure_snapshot_keeps_embedded_failure_reason_after_row_mutation(
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
            "validation_run": {
                "id": "vr_embedded_primary",
                "status": "failed",
                "reason_code": "PYTEST_TEST_FAILURE",
            },
        },
    )

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.validation_failure.value
    assert snapshot["message"] == "pytest failed before cleanup"
    assert snapshot["reason_code"] == "PYTEST_TEST_FAILURE"


@pytest.mark.unit
async def test_primary_failure_snapshot_ignores_stale_embedded_primary_after_resume(
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
            "validation_run": {
                "id": "vr_stale_primary",
                "status": "failed",
                "reason_code": "PYTEST_TEST_FAILURE",
            },
        },
    )

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.monitoring_pr.value
        workspace.failure_reason = None
        workspace.failure_message = None
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is None


@pytest.mark.unit
async def test_epoch_reset_detection_treats_same_timestamp_reset_as_epoch_boundary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    same_tick = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure epoch ordering regression",
            task_prompt="Detect same-timestamp epoch resets.",
            agent="codex",
            test_commands=[],
        )
        failed_event = WorkspaceEvent(
            id="evt_zzzzzzzzzzzzzzzzzzzzzzzz",
            workspace_id=workspace.id,
            event_type="workspace.state_changed",
            old_state=WorkspaceStatus.running.value,
            new_state=WorkspaceStatus.failed.value,
            reason_code="PYTEST_TEST_FAILURE",
            payload={"reason_code": "PYTEST_TEST_FAILURE"},
            occurred_at=same_tick,
            event_order=1,
        )
        reset_event = WorkspaceEvent(
            id="evt_aaaaaaaaaaaaaaaaaaaaaaaa",
            workspace_id=workspace.id,
            event_type="workspace.state_changed",
            old_state=WorkspaceStatus.failed.value,
            new_state=WorkspaceStatus.monitoring_pr.value,
            reason_code="MONITORING_PR",
            payload=None,
            occurred_at=same_tick,
            event_order=2,
        )
        session.add_all([failed_event, reset_event])
        await session.flush()

        assert reset_event.id < failed_event.id
        reset_detected = await failure_causality_service._has_failure_epoch_reset_after(
            session,
            workspace.id,
            failed_event,
        )

    assert reset_detected is True


@pytest.mark.unit
async def test_epoch_reset_detection_ignores_same_tick_reset_when_reference_event_is_unordered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    same_tick = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Failure epoch unordered reference regression",
            task_prompt="Do not drop primary evidence for ambiguous same-tick resets.",
            agent="codex",
            test_commands=[],
        )
        failed_event = WorkspaceEvent(
            id="evt_unordered_failure",
            workspace_id=workspace.id,
            event_type="workspace.state_changed",
            old_state=WorkspaceStatus.running.value,
            new_state=WorkspaceStatus.failed.value,
            reason_code="AGENT_AUTH_FAILED",
            payload={
                "reason_code": "AGENT_AUTH_FAILED",
                "message": "agent auth failed before ambiguous reset",
            },
            occurred_at=same_tick,
            event_order=None,
        )
        reset_event = WorkspaceEvent(
            id="evt_ordered_reset_same_tick",
            workspace_id=workspace.id,
            event_type="workspace.state_changed",
            old_state=WorkspaceStatus.failed.value,
            new_state=WorkspaceStatus.monitoring_pr.value,
            reason_code="MONITORING_PR",
            payload=None,
            occurred_at=same_tick,
            event_order=2,
        )
        session.add_all([failed_event, reset_event])
        workspace.status = WorkspaceStatus.failed.value
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "agent auth failed before ambiguous reset"
        workspace_id = workspace.id
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.agent_failure.value
    assert snapshot["message"] == "agent auth failed before ambiguous reset"
    assert snapshot["reason_code"] == "AGENT_AUTH_FAILED"


@pytest.mark.unit
async def test_epoch_reset_detection_reads_remonitor_state_reset_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_at = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Remonitor reset target regression",
            task_prompt="Detect state_reset target even when event new_state is stale.",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.failed.value
        failed_event = WorkspaceEvent(
            id="evt_primary_failure",
            workspace_id=workspace.id,
            event_type="workspace.state_changed",
            old_state=WorkspaceStatus.running.value,
            new_state=WorkspaceStatus.failed.value,
            reason_code="PYTEST_TEST_FAILURE",
            payload={"reason_code": "PYTEST_TEST_FAILURE"},
            occurred_at=reset_at - timedelta(minutes=5),
        )
        session.add(failed_event)
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
        reset_event.occurred_at = reset_at
        assert reset_event.new_state == WorkspaceStatus.failed.value
        await session.flush()

        reset_detected = await failure_causality_service._has_failure_epoch_reset_after(
            session,
            workspace.id,
            failed_event,
        )

    assert reset_detected is True


@pytest.mark.unit
async def test_remonitor_new_state_resets_failure_epoch_without_state_reset_payload(
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
            "message": "pytest failed before remonitor",
            "reason_code": "PYTEST_TEST_FAILURE",
            "validation_run": {
                "id": "vr_pre_remonitor_primary",
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
        workspace.failure_reason = None
        workspace.failure_message = None
        await repo.add_event_with_states(
            workspace,
            event_type="workspace.remonitor_requested",
            old_state=WorkspaceStatus.failed,
            new_state=WorkspaceStatus.monitoring_pr,
            reason_code="OPERATOR_REMONITOR",
            payload={"reason": "operator requested remonitor"},
        )

        workspace.failure_reason = FailureReason.infrastructure_failure.value
        workspace.failure_message = "stale active scan failed after remonitor"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="STALE_ACTIVE_EXECUTION",
            payload={
                "reason_code": "STALE_ACTIVE_EXECUTION",
                "message": "stale active scan failed after remonitor",
            },
        )
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.infrastructure_failure.value
    assert snapshot["message"] == "stale active scan failed after remonitor"
    assert snapshot["reason_code"] == "STALE_ACTIVE_EXECUTION"
    assert "validation_run" not in snapshot


@pytest.mark.unit
async def test_primary_failure_snapshot_uses_current_failure_after_provisioning_reset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reset_at = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Provisioning epoch reset regression",
            task_prompt="Do not reuse pre-provisioning primary evidence.",
            agent="codex",
            test_commands=[],
        )
        old_failed_event = WorkspaceEvent(
            id="evt_old_provisioning_failure",
            workspace_id=workspace.id,
            event_type="workspace.state_changed",
            old_state=WorkspaceStatus.provisioning.value,
            new_state=WorkspaceStatus.failed.value,
            reason_code="OLD_PROVISIONING_FAILURE",
            payload={
                "primary_failure": {
                    "failure_reason": FailureReason.validation_failure.value,
                    "message": "old provisioning validation failed",
                    "reason_code": "OLD_VALIDATION_FAILURE",
                    "validation_run": {
                        "id": "vr_old_provisioning",
                        "status": "failed",
                        "reason_code": "OLD_VALIDATION_FAILURE",
                    },
                },
            },
            occurred_at=reset_at - timedelta(minutes=10),
        )
        provisioning_reset_event = WorkspaceEvent(
            id="evt_retry_provisioning",
            workspace_id=workspace.id,
            event_type="workspace.state_changed",
            old_state=WorkspaceStatus.failed.value,
            new_state=WorkspaceStatus.provisioning.value,
            reason_code="RETRY_PROVISIONING",
            payload=None,
            occurred_at=reset_at,
        )
        current_failed_event = WorkspaceEvent(
            id="evt_current_agent_failure",
            workspace_id=workspace.id,
            event_type="workspace.state_changed",
            old_state=WorkspaceStatus.provisioning.value,
            new_state=WorkspaceStatus.failed.value,
            reason_code="AGENT_AUTH_FAILED",
            payload={
                "reason_code": "AGENT_AUTH_FAILED",
                "message": "agent auth failed during reprovisioning",
            },
            occurred_at=reset_at + timedelta(minutes=5),
        )
        session.add_all([old_failed_event, provisioning_reset_event, current_failed_event])
        workspace.status = WorkspaceStatus.failed.value
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "agent auth failed during reprovisioning"
        workspace_id = workspace.id
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.agent_failure.value
    assert snapshot["message"] == "agent auth failed during reprovisioning"
    assert snapshot["reason_code"] == "AGENT_AUTH_FAILED"
    assert "validation_run" not in snapshot


@pytest.mark.unit
async def test_primary_failure_snapshot_ignores_same_timestamp_epoch_reset_without_id_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    same_tick = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)
    workspace_id, _validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.infrastructure_failure.value,
        failure_message="cleanup failed after primary validation failure",
        reason_code="CLEANUP_FAILED",
        validation_reason_code="PYTEST_TEST_FAILURE",
        embedded_primary={
            "failure_reason": FailureReason.validation_failure.value,
            "message": "old pytest failure before cleanup",
            "reason_code": "PYTEST_TEST_FAILURE",
            "validation_run": {
                "id": "vr_same_timestamp_stale_primary",
                "status": "failed",
                "reason_code": "PYTEST_TEST_FAILURE",
            },
        },
    )

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        stale_failed_event = next(
            event
            for event in workspace.events
            if event.new_state == WorkspaceStatus.failed.value
            and event.reason_code == "CLEANUP_FAILED"
        )
        stale_failed_event.id = "evt_zzzzzzzzzzzzzzzzzzzzzzzz"
        stale_failed_event.occurred_at = same_tick

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
        reset_event.id = "evt_aaaaaaaaaaaaaaaaaaaaaaaa"
        reset_event.old_state = WorkspaceStatus.failed.value
        reset_event.new_state = WorkspaceStatus.monitoring_pr.value
        reset_event.occurred_at = same_tick
        assert reset_event.id < stale_failed_event.id

        workspace.status = WorkspaceStatus.failed.value
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "agent failed after same-timestamp remonitor reset"
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.agent_failure.value
    assert snapshot["message"] == "agent failed after same-timestamp remonitor reset"
    assert snapshot.get("reason_code") != "PYTEST_TEST_FAILURE"
    assert "validation_run" not in snapshot


@pytest.mark.unit
async def test_primary_failure_snapshot_omits_stale_validation_run_without_current_epoch_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    same_tick = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)
    workspace_id, validation_run_id = await _seed_failed_workspace(
        session_factory,
        failure_reason=FailureReason.validation_failure.value,
        failure_message="old validation failed before same-timestamp remonitor reset",
        reason_code="OLD_VALIDATION_FAILURE",
        validation_reason_code="OLD_VALIDATION_FAILURE",
    )

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        stale_failed_event = next(
            event
            for event in workspace.events
            if event.new_state == WorkspaceStatus.failed.value
            and event.reason_code == "OLD_VALIDATION_FAILURE"
        )
        stale_failed_event.occurred_at = same_tick

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
        reset_event.occurred_at = same_tick

        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = "live validation failure after remonitor reset"
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.validation_failure.value
    assert snapshot["message"] == "live validation failure after remonitor reset"
    assert snapshot.get("validation_run", {}).get("id") != validation_run_id
    assert "validation_run" not in snapshot
    assert "coverage" not in snapshot
    assert "coverage_percent" not in snapshot


@pytest.mark.unit
async def test_primary_failure_snapshot_uses_current_failure_after_remonitor_reset(
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
            "validation_run": {
                "id": "vr_stale_remonitor_primary",
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
        workspace.failure_reason = None
        workspace.failure_message = None
        await repo.add_event(
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

        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "agent retry failed after remonitor"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code="AGENT_AUTH_FAILED",
            payload={
                "reason_code": "AGENT_AUTH_FAILED",
                "message": "agent retry failed after remonitor",
            },
        )
        await session.commit()

    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

        snapshot = await load_primary_failure_snapshot(session, workspace)

    assert snapshot is not None
    assert snapshot["failure_reason"] == FailureReason.agent_failure.value
    assert snapshot["message"] == "agent retry failed after remonitor"
    assert snapshot["reason_code"] == "AGENT_AUTH_FAILED"
    assert "validation_run" not in snapshot
