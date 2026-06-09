import base64
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.api.schemas import OperationResponse
from awf.common.config import get_settings
from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.bounded_list import InvalidBoundedListCursorError
from awf.service.operations import build_operation_list_response, decode_operation_list_cursor
from awf.service.workspaces import OperationRowsPage


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


@pytest.mark.unit
def test_operation_list_response_uses_keyset_cursor_from_last_returned_row() -> None:
    operation = _operation_response()

    response = build_operation_list_response(
        [operation, operation.model_copy(update={"id": "op_next"})],
        limit=1,
        cursor="prevalidated-upstream",
    )

    assert response.cursor == "prevalidated-upstream"
    assert response.next_cursor is not None
    decoded = decode_operation_list_cursor(response.next_cursor)
    assert decoded is not None
    assert decoded.created_at == operation.created_at
    assert decoded.operation_id == operation.id


@pytest.mark.unit
def test_decode_operation_list_cursor_rejects_empty_operation_id() -> None:
    payload = {"c": datetime(2026, 1, 1, tzinfo=UTC).isoformat(), "i": ""}
    cursor = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    with pytest.raises(InvalidBoundedListCursorError):
        decode_operation_list_cursor(cursor)


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
async def test_list_operations_global(authed_client: AsyncClient, sample_data):
    ws1, ws2 = sample_data

    # Test global list
    response = await authed_client.get("/v1/operations")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None
    assert body["has_more"] is False
    assert body["limit"] == 50
    assert body["cursor"] is None

    # Test filter by workspace_id
    response = await authed_client.get(f"/v1/operations?workspace_id={ws1.id}&limit=2")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["limit"] == 2
    assert body["cursor"] is None

    # Test filter by status
    response = await authed_client.get("/v1/operations?status=running")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["status"] == "running"

    # Test filter by type
    response = await authed_client.get("/v1/operations?type=validate")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["type"] == "validate"


@pytest.mark.unit
async def test_list_operations_reports_has_more_when_limit_truncates(
    authed_client: AsyncClient,
    sample_data,
) -> None:
    response = await authed_client.get("/v1/operations?limit=2")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert body["next_cursor"] is not None
    assert body["limit"] == 2
    assert body["cursor"] is None


@pytest.mark.unit
async def test_list_workspace_operations_reports_has_more_when_limit_truncates(
    authed_client: AsyncClient,
    sample_data,
) -> None:
    ws1, _ws2 = sample_data

    response = await authed_client.get(f"/v1/workspaces/{ws1.id}/operations?limit=1")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["has_more"] is True
    assert body["next_cursor"] is not None
    assert body["limit"] == 1
    assert body["cursor"] is None


@pytest.mark.unit
async def test_list_operations_next_cursor_fetches_second_page(
    authed_client: AsyncClient,
    session: AsyncSession,
) -> None:
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)
    workspace = await ws_repo.create(
        repo_url="https://github.com/org/repo_paged_ops",
        branch_base="main",
        task_title="paged operations",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    base = datetime(2026, 1, 1, tzinfo=UTC)
    oldest = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.create,
        status=OperationStatus.succeeded,
    )
    middle = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
    )
    newest = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.rebase,
        status=OperationStatus.pending,
    )
    oldest.created_at = base
    middle.created_at = base + timedelta(seconds=1)
    newest.created_at = base + timedelta(seconds=2)
    await session.commit()

    first_response = await authed_client.get("/v1/operations?limit=1")
    assert first_response.status_code == 200
    first_body = first_response.json()
    assert [item["id"] for item in first_body["items"]] == [newest.id]
    assert first_body["has_more"] is True
    assert first_body["next_cursor"] is not None

    second_response = await authed_client.get(
        "/v1/operations",
        params={"limit": 1, "cursor": first_body["next_cursor"]},
    )

    assert second_response.status_code == 200
    second_body = second_response.json()
    assert [item["id"] for item in second_body["items"]] == [middle.id]
    assert second_body["cursor"] == first_body["next_cursor"]
    assert second_body["next_cursor"] is not None


@pytest.mark.unit
async def test_list_operations_cursor_is_stable_when_newer_operations_arrive(
    authed_client: AsyncClient,
    session: AsyncSession,
) -> None:
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)
    workspace = await ws_repo.create(
        repo_url="https://github.com/org/repo_live_ops",
        branch_base="main",
        task_title="live operations",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    base = datetime(2026, 1, 1, tzinfo=UTC)
    oldest = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.create,
        status=OperationStatus.succeeded,
    )
    middle = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
    )
    newest = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.rebase,
        status=OperationStatus.pending,
    )
    oldest.created_at = base
    middle.created_at = base + timedelta(seconds=1)
    newest.created_at = base + timedelta(seconds=2)
    await session.commit()

    first_response = await authed_client.get(
        "/v1/operations",
        params={"workspace_id": workspace.id, "limit": 1},
    )
    assert first_response.status_code == 200
    first_body = first_response.json()
    assert [item["id"] for item in first_body["items"]] == [newest.id]

    inserted = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.stop,
        status=OperationStatus.pending,
    )
    inserted.created_at = base + timedelta(seconds=3)
    await session.commit()

    second_response = await authed_client.get(
        "/v1/operations",
        params={
            "workspace_id": workspace.id,
            "limit": 1,
            "cursor": first_body["next_cursor"],
        },
    )

    assert second_response.status_code == 200
    second_body = second_response.json()
    assert [item["id"] for item in second_body["items"]] == [middle.id]


@pytest.mark.unit
async def test_list_workspace_operations_cursor_is_stable_when_newer_operations_arrive(
    authed_client: AsyncClient,
    session: AsyncSession,
) -> None:
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)
    workspace = await ws_repo.create(
        repo_url="https://github.com/org/repo_live_workspace_ops",
        branch_base="main",
        task_title="live workspace operations",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    base = datetime(2026, 1, 1, tzinfo=UTC)
    oldest = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.create,
        status=OperationStatus.succeeded,
    )
    middle = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.validate,
        status=OperationStatus.running,
    )
    newest = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.rebase,
        status=OperationStatus.pending,
    )
    oldest.created_at = base
    middle.created_at = base + timedelta(seconds=1)
    newest.created_at = base + timedelta(seconds=2)
    await session.commit()

    first_response = await authed_client.get(f"/v1/workspaces/{workspace.id}/operations?limit=1")
    assert first_response.status_code == 200
    first_body = first_response.json()
    assert [item["id"] for item in first_body["items"]] == [newest.id]

    inserted = await op_repo.create(
        workspace_id=workspace.id,
        operation_type=OperationType.stop,
        status=OperationStatus.pending,
    )
    inserted.created_at = base + timedelta(seconds=3)
    await session.commit()

    second_response = await authed_client.get(
        f"/v1/workspaces/{workspace.id}/operations",
        params={"limit": 1, "cursor": first_body["next_cursor"]},
    )

    assert second_response.status_code == 200
    second_body = second_response.json()
    assert [item["id"] for item in second_body["items"]] == [middle.id]


@pytest.mark.unit
async def test_list_operations_invalid_cursor_returns_structured_400(
    authed_client: AsyncClient,
) -> None:
    response = await authed_client.get("/v1/operations?cursor=not-a-cursor")

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_CURSOR",
        "message": "Invalid operation list cursor.",
    }


@pytest.mark.unit
async def test_list_workspace_operations_invalid_cursor_returns_structured_400(
    authed_client: AsyncClient,
    sample_data,
) -> None:
    ws1, _ws2 = sample_data

    response = await authed_client.get(f"/v1/workspaces/{ws1.id}/operations?cursor=not-a-cursor")

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_CURSOR",
        "message": "Invalid operation list cursor.",
    }


@pytest.mark.unit
async def test_list_operations_uses_prevalidated_service_responses(
    authed_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = _operation_response()

    class PrevalidatedOperationService:
        def __init__(self, session_factory) -> None:  # type: ignore[no-untyped-def]
            self.session_factory = session_factory

        async def list_all_operations_page(
            self,
            **kwargs: object,
        ) -> OperationRowsPage:
            return OperationRowsPage(rows=[operation])

    def fail_model_validate(cls, value) -> OperationResponse:  # type: ignore[no-untyped-def]
        raise AssertionError("OperationResponse.model_validate should not be called")

    monkeypatch.setattr(
        "awf.api.routes.operations.WorkspaceService",
        PrevalidatedOperationService,
    )
    monkeypatch.setattr(OperationResponse, "model_validate", classmethod(fail_model_validate))

    response = await authed_client.get("/v1/operations")

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == operation.id


@pytest.mark.unit
async def test_list_workspace_operations_filters(authed_client: AsyncClient, sample_data):
    ws1, ws2 = sample_data

    # Test workspace-scoped list with status filter
    response = await authed_client.get(f"/v1/workspaces/{ws1.id}/operations?status=succeeded")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["type"] == "create"
    assert body["next_cursor"] is None
    assert body["has_more"] is False
    assert body["limit"] == 50
    assert body["cursor"] is None

    # Test workspace-scoped list with type filter
    response = await authed_client.get(f"/v1/workspaces/{ws1.id}/operations?type=validate")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["status"] == "running"


@pytest.mark.unit
async def test_list_workspace_operations_not_found(authed_client: AsyncClient):
    response = await authed_client.get("/v1/workspaces/ws_missing/operations")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_get_operation_not_found(authed_client: AsyncClient):
    response = await authed_client.get("/v1/operations/op_missing")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "error_code": "NOT_FOUND",
        "message": "No operation with id op_missing",
    }


@pytest.mark.unit
async def test_operation_response_serializes_stable_audit_fields(
    authed_client: AsyncClient,
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

    detail_response = await authed_client.get(f"/v1/operations/{operation.id}")
    list_response = await authed_client.get(f"/v1/operations?workspace_id={ws.id}")

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
    authed_client: AsyncClient,
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

    detail_response = await authed_client.get(f"/v1/operations/{operation.id}")
    list_response = await authed_client.get(f"/v1/workspaces/{ws.id}/operations")

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
    authed_client: AsyncClient,
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

    global_response = await authed_client.get(f"/v1/operations?workspace_id={ws.id}")
    global_filter_response = await authed_client.get("/v1/operations?type=monitor_state")
    workspace_response = await authed_client.get(f"/v1/workspaces/{ws.id}/operations")
    workspace_filter_response = await authed_client.get(
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
    authed_client: AsyncClient,
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

    response = await authed_client.get(f"/v1/operations/{operation.id}")

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
    authed_client: AsyncClient,
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

    response = await authed_client.get(f"/v1/operations/{operation.id}")

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
def test_operation_response_deduplicates_colliding_log_stream_ref_lists() -> None:
    now = datetime(2026, 4, 28, tzinfo=UTC)

    response = OperationResponse(
        id="op_duplicate_logs",
        workspace_id="ws_duplicate_logs",
        type=OperationType.validate.value,
        status=OperationStatus.succeeded.value,
        error_code=None,
        error_message=None,
        payload={"log_stream_refs": {"monitor": "monitor.log"}},
        result={"log_stream_refs": {"monitor": ["monitor.log", "monitor.retry.log"]}},
        idempotency_key=None,
        created_at=now,
        started_at=now,
        finished_at=now,
    )

    assert response.log_stream_refs == {"monitor": ["monitor.log", "monitor.retry.log"]}
    assert response.log_stream_ids == ["monitor.log", "monitor.retry.log"]


@pytest.mark.unit
async def test_list_operations_limit_validation(authed_client: AsyncClient):
    response = await authed_client.get("/v1/operations?limit=0")
    assert response.status_code == 422

    response = await authed_client.get("/v1/operations?limit=501")
    assert response.status_code == 422


@pytest.mark.unit
async def test_list_operations_ordering(authed_client: AsyncClient, session: AsyncSession):
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

    response = await authed_client.get(f"/v1/operations?workspace_id={ws.id}")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 3
    # Newest first
    assert items[0]["id"] == op3.id
    assert items[1]["id"] == op2.id
    assert items[2]["id"] == op1.id


@pytest.mark.unit
async def test_list_operations_empty(authed_client: AsyncClient, session: AsyncSession):
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
    response = await authed_client.get(f"/v1/operations?workspace_id={ws.id}&type=validate")
    assert response.status_code == 200
    assert response.json()["items"] == []

    # Empty workspace list
    response = await authed_client.get(f"/v1/workspaces/{ws.id}/operations")
    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.fixture
def authed_client(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncClient]:
    get_settings.cache_clear()
    monkeypatch.setenv("AWF_API_TOKEN", "test-token")
    original_auth = client.headers.get("Authorization")
    client.headers["Authorization"] = "Bearer test-token"
    try:
        yield client
    finally:
        if original_auth is None:
            client.headers.pop("Authorization", None)
        else:
            client.headers["Authorization"] = original_auth
        get_settings.cache_clear()


@pytest.mark.unit
async def test_unauthenticated_operation_reads_rejected(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, sample_data
) -> None:
    ws1, _ = sample_data

    try:
        get_settings.cache_clear()
        monkeypatch.setenv("AWF_API_TOKEN", "")
        for path in [
            "/v1/operations",
            "/v1/operations/op_missing",
            f"/v1/workspaces/{ws1.id}/operations",
        ]:
            response = await client.get(path)
            assert response.status_code == 503

        get_settings.cache_clear()
        monkeypatch.setenv("AWF_API_TOKEN", "test-token")
        for path in [
            "/v1/operations",
            "/v1/operations/op_missing",
            f"/v1/workspaces/{ws1.id}/operations",
        ]:
            response = await client.get(path)
            assert response.status_code == 401
    finally:
        get_settings.cache_clear()


@pytest.mark.unit
async def test_authenticated_operation_reads_accepted(
    authed_client: AsyncClient, sample_data
) -> None:
    ws1, _ = sample_data

    for path in [
        "/v1/operations",
        "/v1/operations/op_missing",
        f"/v1/workspaces/{ws1.id}/operations",
    ]:
        response = await authed_client.get(path)
        assert response.status_code in (200, 404)
