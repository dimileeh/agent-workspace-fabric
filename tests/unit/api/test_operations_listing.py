from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.api.schemas import OperationResponse
from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory


def _operation_response() -> OperationResponse:
    return OperationResponse(
        id="op_prevalidated",
        workspace_id="ws_prevalidated",
        type="validate",
        status="succeeded",
        error_code=None,
        error_message=None,
        payload=None,
        result=None,
        idempotency_key=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=None,
        finished_at=None,
    )


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = make_session_factory(engine)
    async with factory() as s:
        yield s


@pytest.fixture
async def sample_data(session: AsyncSession):
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)

    ws1 = await ws_repo.create(
        repo_url="https://github.com/org/repo1",
        branch_base="main",
        task_title="task1",
        task_prompt="prompt1",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    ws2 = await ws_repo.create(
        repo_url="https://github.com/org/repo2",
        branch_base="main",
        task_title="task2",
        task_prompt="prompt2",
        agent="claude-3-sonnet",
        test_commands=[],
    )

    # op1: ws1, create, succeeded
    await op_repo.create(
        workspace_id=ws1.id, operation_type=OperationType.create, status=OperationStatus.succeeded
    )
    # op2: ws1, validate, running
    await op_repo.create(
        workspace_id=ws1.id, operation_type=OperationType.validate, status=OperationStatus.running
    )
    # op3: ws2, create, succeeded
    await op_repo.create(
        workspace_id=ws2.id, operation_type=OperationType.create, status=OperationStatus.succeeded
    )

    await session.commit()
    return ws1, ws2


@pytest.mark.unit
async def test_list_operations_global(client: AsyncClient, sample_data):
    ws1, ws2 = sample_data

    # Test global list
    response = await client.get("/v1/operations")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None
    assert body["has_more"] is False
    assert body["limit"] == 50
    assert body["cursor"] is None

    # Test filter by workspace_id
    response = await client.get(f"/v1/operations?workspace_id={ws1.id}&limit=2")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["limit"] == 2
    assert body["cursor"] is None

    # Test filter by status
    response = await client.get("/v1/operations?status=running")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["status"] == "running"

    # Test filter by type
    response = await client.get("/v1/operations?type=validate")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["type"] == "validate"


@pytest.mark.unit
async def test_list_operations_uses_prevalidated_service_responses(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = _operation_response()

    class PrevalidatedOperationService:
        def __init__(self, session_factory) -> None:  # type: ignore[no-untyped-def]
            self.session_factory = session_factory

        async def list_all_operations(self, **kwargs) -> list[OperationResponse]:  # type: ignore[no-untyped-def]
            return [operation]

    def fail_model_validate(cls, value) -> OperationResponse:  # type: ignore[no-untyped-def]
        raise AssertionError("OperationResponse.model_validate should not be called")

    monkeypatch.setattr(
        "awf.api.routes.operations.WorkspaceService",
        PrevalidatedOperationService,
    )
    monkeypatch.setattr(OperationResponse, "model_validate", classmethod(fail_model_validate))

    response = await client.get("/v1/operations")

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == operation.id


@pytest.mark.unit
async def test_list_workspace_operations_filters(client: AsyncClient, sample_data):
    ws1, ws2 = sample_data

    # Test workspace-scoped list with status filter
    response = await client.get(f"/v1/workspaces/{ws1.id}/operations?status=succeeded")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["type"] == "create"
    assert body["next_cursor"] is None
    assert body["has_more"] is False
    assert body["limit"] == 50
    assert body["cursor"] is None

    # Test workspace-scoped list with type filter
    response = await client.get(f"/v1/workspaces/{ws1.id}/operations?type=validate")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["status"] == "running"


@pytest.mark.unit
async def test_list_workspace_operations_not_found(client: AsyncClient):
    response = await client.get("/v1/workspaces/ws_missing/operations")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_get_operation_not_found(client: AsyncClient):
    response = await client.get("/v1/operations/op_missing")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "error_code": "NOT_FOUND",
        "message": "No operation with id op_missing",
    }


@pytest.mark.unit
async def test_operation_response_serializes_stable_audit_fields(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)
    ws = await ws_repo.create(
        repo_url="https://github.com/org/repo_audit",
        branch_base="main",
        task_title="audit operation",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    operation = await op_repo.create(
        workspace_id=ws.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
        payload={
            "owner": "operator_api",
            "source": "operator_api",
            "reason": "rerun checks",
            "reason_code": "OPERATOR_VALIDATE",
            "log_stream_refs": {"monitor": "monitor.log"},
        },
    )
    await op_repo.finish(
        operation,
        status=OperationStatus.succeeded,
        result={
            "status": "validated",
            "log_stream_refs": {
                "validation": {
                    "stdout": "validation.01_validate.stdout",
                    "stderr": "validation.01_validate.stderr",
                }
            },
        },
    )
    await session.commit()

    detail_response = await client.get(f"/v1/operations/{operation.id}")
    list_response = await client.get(f"/v1/operations?workspace_id={ws.id}")

    assert detail_response.status_code == 200
    assert list_response.status_code == 200
    detail = detail_response.json()
    listed = list_response.json()["items"][0]
    for item in (detail, listed):
        assert item["owner"] == "operator_api"
        assert item["source"] == "operator_api"
        assert item["reason"] == "rerun checks"
        assert item["reason_code"] == "OPERATOR_VALIDATE"
        assert item["failure_code"] is None
        assert item["failure_message"] is None
        assert item["log_stream_refs"] == {
            "monitor": "monitor.log",
            "validation": {
                "stdout": "validation.01_validate.stdout",
                "stderr": "validation.01_validate.stderr",
            },
        }
        assert item["log_stream_ids"] == [
            "monitor.log",
            "validation.01_validate.stderr",
            "validation.01_validate.stdout",
        ]
        assert item["payload"] == operation.payload
        assert item["result"] == operation.result


@pytest.mark.unit
async def test_operation_response_derives_pr_monitor_recovery_fields(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)
    ws = await ws_repo.create(
        repo_url="https://github.com/org/repo_monitor",
        branch_base="main",
        task_title="monitor recovery operation",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    operation = await op_repo.create(
        workspace_id=ws.id,
        operation_type=OperationType.validate,
        status=OperationStatus.pending,
        payload={
            "owner": "pr_monitor",
            "source": "pr_monitor",
            "action": "validate_only",
            "requested_action": "validate",
            "reason": "Required validation tier has not passed.",
            "reason_code": "VALIDATION_INSUFFICIENT_TIER",
            "pr_number": 42,
            "pr_url": "https://github.com/org/repo_monitor/pull/42",
            "source_head_sha": "c" * 40,
            "source_base_sha": "b" * 40,
        },
        idempotency_key="pr_monitor:validate_only:test",
    )
    await session.commit()

    detail_response = await client.get(f"/v1/operations/{operation.id}")
    list_response = await client.get(f"/v1/workspaces/{ws.id}/operations")

    assert detail_response.status_code == 200
    assert list_response.status_code == 200
    for item in (detail_response.json(), list_response.json()["items"][0]):
        assert item["action"] == "validate_only"
        assert item["pr_number"] == 42
        assert item["pr_url"] == "https://github.com/org/repo_monitor/pull/42"
        assert item["source_head_sha"] == "c" * 40
        assert item["source_base_sha"] == "b" * 40
        assert item["owner"] == "pr_monitor"
        assert item["source"] == "pr_monitor"
        assert item["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"
        assert item["payload"] == operation.payload


@pytest.mark.unit
async def test_monitor_state_operations_list_and_filter_through_existing_endpoints(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)
    ws = await ws_repo.create(
        repo_url="https://github.com/org/repo_monitor_state",
        branch_base="main",
        task_title="monitor state operation",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    operation = await op_repo.create(
        workspace_id=ws.id,
        operation_type=OperationType.monitor_state,
        status=OperationStatus.succeeded,
        payload={
            "owner": "pr_monitor",
            "source": "pr_monitor",
            "action": "merge_ready",
            "requested_action": "merge",
            "reason": "All gates are clean.",
            "reason_code": "MERGE_READY",
            "pr_number": 44,
            "pr_url": "https://github.com/org/repo_monitor_state/pull/44",
            "source_head_sha": "d" * 40,
            "source_base_sha": "e" * 40,
        },
    )
    await session.commit()

    global_response = await client.get(f"/v1/operations?workspace_id={ws.id}")
    global_filter_response = await client.get("/v1/operations?type=monitor_state")
    workspace_response = await client.get(f"/v1/workspaces/{ws.id}/operations")
    workspace_filter_response = await client.get(
        f"/v1/workspaces/{ws.id}/operations?type=monitor_state"
    )

    for response in (
        global_response,
        global_filter_response,
        workspace_response,
        workspace_filter_response,
    ):
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["id"] for item in items] == [operation.id]
        assert items[0]["type"] == "monitor_state"
        assert items[0]["action"] == "merge_ready"
        assert items[0]["owner"] == "pr_monitor"
        assert items[0]["reason_code"] == "MERGE_READY"


@pytest.mark.unit
async def test_legacy_operation_without_payload_or_result_serializes_audit_fields(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)
    ws = await ws_repo.create(
        repo_url="https://github.com/org/repo_legacy_operation",
        branch_base="main",
        task_title="legacy operation",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    operation = await op_repo.create(
        workspace_id=ws.id,
        operation_type=OperationType.create,
        status=OperationStatus.succeeded,
        payload=None,
    )
    await session.commit()

    response = await client.get(f"/v1/operations/{operation.id}")

    assert response.status_code == 200
    item = response.json()
    assert item["payload"] is None
    assert item["result"] is None
    assert item["owner"] is None
    assert item["source"] is None
    assert item["action"] is None
    assert item["reason"] is None
    assert item["reason_code"] is None
    assert item["failure_code"] is None
    assert item["failure_message"] is None
    assert item["log_stream_refs"] == {}
    assert item["log_stream_ids"] == []


@pytest.mark.unit
async def test_operation_response_derives_failure_fields_from_error_columns(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)
    ws = await ws_repo.create(
        repo_url="https://github.com/org/repo_failure_audit",
        branch_base="main",
        task_title="audit failed operation",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    operation = await op_repo.create(
        workspace_id=ws.id,
        operation_type=OperationType.stop,
        status=OperationStatus.running,
        payload={"source": "operator_api", "reason_code": "OPERATOR_STOP"},
    )
    await op_repo.finish(
        operation,
        status=OperationStatus.failed,
        error_code="STACK_STOP_FAILED",
        error_message="docker stop failed",
    )
    await session.commit()

    response = await client.get(f"/v1/operations/{operation.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["failure_code"] == "STACK_STOP_FAILED"
    assert body["failure_message"] == "docker stop failed"
    assert body["error_code"] == "STACK_STOP_FAILED"
    assert body["error_message"] == "docker stop failed"
    assert body["log_stream_refs"] == {}
    assert body["log_stream_ids"] == []


@pytest.mark.unit
def test_operation_response_extracts_log_stream_ids_from_nested_lists() -> None:
    now = datetime(2026, 4, 28, tzinfo=UTC)

    response = OperationResponse(
        id="op_nested_logs",
        workspace_id="ws_nested_logs",
        type=OperationType.validate.value,
        status=OperationStatus.succeeded.value,
        error_code=None,
        error_message=None,
        payload={
            "source": "operator_api",
            "log_stream_refs": {
                "commands": [
                    {"stdout": "validation.01_validate.stdout"},
                    {"stderr": "validation.01_validate.stderr"},
                ]
            },
        },
        result=None,
        idempotency_key=None,
        created_at=now,
        started_at=now,
        finished_at=now,
    )

    assert response.log_stream_ids == [
        "validation.01_validate.stderr",
        "validation.01_validate.stdout",
    ]

    empty_response = OperationResponse(
        id="op_empty_logs",
        workspace_id="ws_empty_logs",
        type=OperationType.validate.value,
        status=OperationStatus.succeeded.value,
        error_code=None,
        error_message=None,
        payload={"log_stream_refs": {"commands": []}},
        result=None,
        idempotency_key=None,
        created_at=now,
        started_at=now,
        finished_at=now,
    )

    assert empty_response.log_stream_ids == []

    non_stream_response = OperationResponse(
        id="op_non_stream_logs",
        workspace_id="ws_non_stream_logs",
        type=OperationType.validate.value,
        status=OperationStatus.succeeded.value,
        error_code=None,
        error_message=None,
        payload={"log_stream_refs": {"bytes": 123}},
        result=None,
        idempotency_key=None,
        created_at=now,
        started_at=now,
        finished_at=now,
    )

    assert non_stream_response.log_stream_ids == []


@pytest.mark.unit
def test_operation_response_ignores_log_stream_ids_past_depth_limit() -> None:
    now = datetime(2026, 4, 28, tzinfo=UTC)
    refs: dict[str, object] = {"stream": "validation.stdout"}
    for _ in range(1200):
        refs = {"nested": refs}

    response = OperationResponse(
        id="op_deep_logs",
        workspace_id="ws_deep_logs",
        type=OperationType.validate.value,
        status=OperationStatus.succeeded.value,
        error_code=None,
        error_message=None,
        payload={"log_stream_refs": refs},
        result=None,
        idempotency_key=None,
        created_at=now,
        started_at=now,
        finished_at=now,
    )

    assert response.log_stream_ids == []


@pytest.mark.unit
def test_operation_response_preserves_colliding_log_stream_refs() -> None:
    now = datetime(2026, 4, 28, tzinfo=UTC)

    response = OperationResponse(
        id="op_colliding_logs",
        workspace_id="ws_colliding_logs",
        type=OperationType.validate.value,
        status=OperationStatus.succeeded.value,
        error_code=None,
        error_message=None,
        payload={
            "log_stream_refs": {
                "commands": {"stdout": "payload.commands.stdout"},
                "monitor": "payload.monitor.log",
            },
        },
        result={
            "log_stream_refs": {
                "commands": {"stderr": "result.commands.stderr"},
                "monitor": "result.monitor.log",
            },
        },
        idempotency_key=None,
        created_at=now,
        started_at=now,
        finished_at=now,
    )

    assert response.log_stream_refs == {
        "commands": {
            "stdout": "payload.commands.stdout",
            "stderr": "result.commands.stderr",
        },
        "monitor": ["payload.monitor.log", "result.monitor.log"],
    }
    assert response.log_stream_ids == [
        "payload.commands.stdout",
        "payload.monitor.log",
        "result.commands.stderr",
        "result.monitor.log",
    ]


@pytest.mark.unit
async def test_list_operations_limit_validation(client: AsyncClient):
    response = await client.get("/v1/operations?limit=0")
    assert response.status_code == 422

    response = await client.get("/v1/operations?limit=501")
    assert response.status_code == 422


@pytest.mark.unit
async def test_list_operations_ordering(client: AsyncClient, session: AsyncSession):
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)

    ws = await ws_repo.create(
        repo_url="https://github.com/org/repo_order",
        branch_base="main",
        task_title="task",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )

    op1 = await op_repo.create(workspace_id=ws.id, operation_type=OperationType.create)
    op2 = await op_repo.create(workspace_id=ws.id, operation_type=OperationType.validate)
    op3 = await op_repo.create(workspace_id=ws.id, operation_type=OperationType.start)
    base_created_at = datetime(2026, 1, 1, tzinfo=UTC)
    op1.created_at = base_created_at
    op2.created_at = base_created_at + timedelta(seconds=1)
    op3.created_at = base_created_at + timedelta(seconds=2)

    await session.commit()

    response = await client.get(f"/v1/operations?workspace_id={ws.id}")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 3
    # Newest first
    assert items[0]["id"] == op3.id
    assert items[1]["id"] == op2.id
    assert items[2]["id"] == op1.id


@pytest.mark.unit
async def test_list_operations_empty(client: AsyncClient, session: AsyncSession):
    ws_repo = WorkspaceRepository(session)
    ws = await ws_repo.create(
        repo_url="https://github.com/org/repo_empty",
        branch_base="main",
        task_title="task",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    await session.commit()

    # Empty global list scoped to this workspace
    response = await client.get(f"/v1/operations?workspace_id={ws.id}&type=validate")
    assert response.status_code == 200
    assert response.json()["items"] == []

    # Empty workspace list
    response = await client.get(f"/v1/workspaces/{ws.id}/operations")
    assert response.status_code == 200
    assert response.json()["items"] == []
