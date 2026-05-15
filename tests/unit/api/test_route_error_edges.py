"""Focused API route error-edge tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import awf.api.routes.artifacts as artifact_routes
import awf.api.routes.validation as validation_routes
import awf.api.routes.workspaces as workspace_routes
from awf.api.schemas import WorkspaceCreateRequest, WorkspaceCreateV2Request
from awf.db.repositories import TaskExternalIdConflictError
from awf.service import workspaces as workspaces_service
from awf.service.bounded_list import InvalidBoundedListCursorError
from awf.service.disk import DiskCheck


def _admission_ok_disk_check() -> DiskCheck:
    return DiskCheck(
        path="/tmp",
        checked_path="/tmp",
        total_bytes=100,
        used_bytes=1,
        free_bytes=99,
        percent_free=99.0,
        threshold_bytes=1,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
    )


@pytest.mark.unit
async def test_workspace_v1_create_acquires_idempotency_lock_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    created_at = datetime(2026, 1, 1, tzinfo=UTC)

    class _Repository:
        def __init__(self, _session: object) -> None:
            return None

        async def acquire_idempotency_key_lock(self, key: str) -> None:
            calls.append(("lock", key))

        async def get_by_idempotency_key(self, key: str) -> object | None:
            calls.append(("get", key))
            return None

        async def create(self, **kwargs: object) -> object:
            calls.append(("create", str(kwargs["idempotency_key"])))
            return SimpleNamespace(
                id="ws_locked_v1",
                status="requested",
                version=1,
                created_at=created_at,
            )

    monkeypatch.setattr(workspace_routes, "WorkspaceRepository", _Repository)

    response = await workspace_routes.create_workspace(
        WorkspaceCreateRequest(
            repo_url="https://github.com/example/repo.git",
            branch_base="main",
            task_title="Serialize REST v1 idempotency",
            task_prompt="exercise lock ordering",
        ),
        idempotency_key="route-v1-key",
        session=object(),  # type: ignore[arg-type]
    )

    assert response.workspace_id == "ws_locked_v1"
    assert calls[:2] == [("lock", "route-v1-key"), ("get", "route-v1-key")]


@pytest.mark.unit
async def test_workspace_v2_create_acquires_idempotency_lock_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    created_at = datetime(2026, 1, 1, tzinfo=UTC)

    class _Repository:
        def __init__(self, _session: object) -> None:
            return None

        async def acquire_idempotency_key_lock(self, key: str) -> None:
            calls.append(("lock", key))

        async def get_by_idempotency_key(self, key: str) -> object | None:
            calls.append(("get", key))
            return None

    async def create_row(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            id="ws_locked_v2",
            status="requested",
            version=1,
            created_at=created_at,
        )

    monkeypatch.setattr(workspace_routes, "WorkspaceRepository", _Repository)
    monkeypatch.setattr(workspace_routes, "create_workspace_v2_row", create_row)
    monkeypatch.setattr(workspace_routes, "owned_path_overlap_warnings", lambda _ws: [])
    monkeypatch.setattr(
        workspace_routes,
        "workspace_provider_readiness_preflight",
        lambda _ws: None,
    )
    monkeypatch.setattr(
        workspace_routes,
        "_workspace_admission_disk_check",
        AsyncMock(return_value=_admission_ok_disk_check()),
    )

    response = await workspace_routes.create_workspace_v2(
        WorkspaceCreateV2Request(
            repo={"url": "https://github.com/example/repo.git", "base_branch": "main"},
            task={"title": "Serialize REST v2 idempotency", "prompt": "exercise lock ordering"},
        ),
        request=SimpleNamespace(),  # type: ignore[arg-type]
        idempotency_key="route-v2-key",
        settings=SimpleNamespace(),  # type: ignore[arg-type]
        session=object(),  # type: ignore[arg-type]
    )

    assert response.workspace_id == "ws_locked_v2"
    assert calls[:2] == [("lock", "route-v2-key"), ("get", "route-v2-key")]


@pytest.mark.unit
async def test_artifact_list_route_reports_invalid_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        artifact_routes,
        "list_workspace_artifacts_metadata",
        AsyncMock(side_effect=InvalidBoundedListCursorError("bad cursor")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await artifact_routes.list_workspace_artifacts(
            "ws_artifacts",
            cursor="bad",
            session=object(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error_code"] == "INVALID_CURSOR"


@pytest.mark.unit
async def test_validation_provenance_route_reports_invalid_cursor_and_missing_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_mock = AsyncMock(side_effect=InvalidBoundedListCursorError("bad cursor"))
    monkeypatch.setattr(validation_routes, "list_validation_provenance_response", list_mock)

    with pytest.raises(HTTPException) as invalid_cursor:
        await validation_routes.list_validation_provenance(
            "ws_validation",
            cursor="bad",
            session=object(),  # type: ignore[arg-type]
        )
    assert invalid_cursor.value.status_code == 400
    assert invalid_cursor.value.detail["error_code"] == "INVALID_CURSOR"

    list_mock.side_effect = None
    list_mock.return_value = None
    with pytest.raises(HTTPException) as missing_workspace:
        await validation_routes.list_validation_provenance(
            "ws_missing",
            session=object(),  # type: ignore[arg-type]
        )
    assert missing_workspace.value.status_code == 404
    assert missing_workspace.value.detail["error_code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_workspace_stale_reason_route_reports_cursor_and_missing_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_mock = AsyncMock(side_effect=InvalidBoundedListCursorError("bad cursor"))
    monkeypatch.setattr(workspace_routes, "list_workspace_stale_reasons_response", list_mock)

    with pytest.raises(HTTPException) as invalid_cursor:
        await workspace_routes.list_workspace_stale_reasons(
            "ws_stale",
            cursor="bad",
            session=object(),  # type: ignore[arg-type]
        )
    assert invalid_cursor.value.status_code == 400
    assert invalid_cursor.value.detail["error_code"] == "INVALID_CURSOR"

    list_mock.side_effect = None
    list_mock.return_value = None
    with pytest.raises(HTTPException) as missing_workspace:
        await workspace_routes.list_workspace_stale_reasons(
            "ws_missing",
            session=object(),  # type: ignore[arg-type]
        )
    assert missing_workspace.value.status_code == 404
    assert missing_workspace.value.detail["error_code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_workspace_secret_leases_route_reports_missing_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Repository:
        def __init__(self, _session: object) -> None:
            return None

        async def exists(self, workspace_id: str) -> bool:
            assert workspace_id == "ws_missing"
            return False

    monkeypatch.setattr(workspace_routes, "WorkspaceRepository", _Repository)

    with pytest.raises(HTTPException) as missing_workspace:
        await workspace_routes.get_workspace_secret_leases(
            "ws_missing",
            session=object(),  # type: ignore[arg-type]
        )

    assert missing_workspace.value.status_code == 404
    assert missing_workspace.value.detail["error_code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_workspace_v2_create_reports_task_external_id_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = WorkspaceCreateV2Request(
        repo={"url": "https://github.com/example/repo.git", "base_branch": "main"},
        task={"title": "conflict", "prompt": "implement it", "external_id": "task-123"},
    )

    async def raise_conflict(*_args: object, **_kwargs: object) -> object:
        raise TaskExternalIdConflictError("task-123")

    class _Repository:
        def __init__(self, _session: object) -> None:
            return None

        async def get_by_idempotency_key(self, _key: str) -> object | None:
            return None

    monkeypatch.setattr(workspace_routes, "WorkspaceRepository", _Repository)
    monkeypatch.setattr(workspace_routes, "create_workspace_v2_row", raise_conflict)
    monkeypatch.setattr(
        workspace_routes,
        "_workspace_admission_disk_check",
        AsyncMock(return_value=_admission_ok_disk_check()),
    )

    response = await workspace_routes.create_workspace_v2(
        payload,
        request=SimpleNamespace(),  # type: ignore[arg-type]
        idempotency_key=None,
        settings=SimpleNamespace(),  # type: ignore[arg-type]
        session=object(),  # type: ignore[arg-type]
    )

    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["error_code"] == "TASK_EXTERNAL_ID_CONFLICT"
    assert body["message"] == (
        "External task ID is already associated with a different "
        "repo/base/task-class/owned-path scope; use a unique external "
        "task ID for this backlog slice or retry the original scope."
    )
    assert body["detail"] == {"external_id": "task-123"}
    assert "task_external_id" not in json.dumps(body, sort_keys=True)


@pytest.mark.unit
def test_workspace_override_reason_matching_uses_redacted_parts_as_wildcards() -> None:
    assert not workspaces_service._override_reasons_match(  # noqa: SLF001
        "operator checked <redacted> manually",
        None,
        stored_redaction_parts=["operator checked ", " manually"],
    )
    assert workspaces_service._override_reasons_match(  # noqa: SLF001
        "operator checked <redacted> manually",
        "operator checked rotated-token-value manually",
        stored_redaction_parts=["operator checked ", " manually"],
    )
    assert not workspaces_service._override_reasons_match(  # noqa: SLF001
        "operator checked <redacted> manually",
        "operator checked rotated-token-value manually",
        stored_redaction_parts=["stale prefix ", " manually"],
    )
    assert not workspaces_service._override_reasons_match(  # noqa: SLF001
        "operator checked <redacted> manually",
        "operator checked rotated-token-value manually",
        stored_redaction_parts=None,
    )


@pytest.mark.unit
def test_workspace_stored_provider_readiness_override_handles_sparse_snapshots() -> None:
    no_preflight = SimpleNamespace(task_policy={})
    legacy_override = SimpleNamespace(
        task_policy={
            "provider_readiness_preflight": {
                "override_used": True,
                "override_reason": 42,
            }
        }
    )
    redacted_override = SimpleNamespace(
        task_policy={
            "provider_readiness_preflight": {
                "override_requested": True,
                "override_reason": "operator checked <redacted>",
                "override_reason_redaction_parts": ["operator checked ", ""],
            }
        }
    )
    malformed_parts = SimpleNamespace(
        task_policy={
            "provider_readiness_preflight": {
                "override_requested": True,
                "override_reason_redaction_parts": ["only-one-part"],
            }
        }
    )

    assert workspaces_service._stored_task_provider_readiness_override(  # noqa: SLF001
        no_preflight  # type: ignore[arg-type]
    ) == (False, None)
    assert (
        workspaces_service._stored_task_provider_readiness_override_redaction_parts(  # noqa: SLF001
            no_preflight  # type: ignore[arg-type]
        )
        is None
    )
    assert workspaces_service._stored_task_provider_readiness_override(  # noqa: SLF001
        legacy_override  # type: ignore[arg-type]
    ) == (True, None)
    assert workspaces_service._stored_task_provider_readiness_override_redaction_parts(  # noqa: SLF001
        redacted_override  # type: ignore[arg-type]
    ) == ["operator checked ", ""]
    assert (
        workspaces_service._stored_task_provider_readiness_override_redaction_parts(  # noqa: SLF001
            malformed_parts  # type: ignore[arg-type]
        )
        is None
    )
