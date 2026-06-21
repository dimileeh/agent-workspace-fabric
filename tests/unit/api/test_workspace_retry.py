"""Retry/requeue API contract tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.workspaces as workspaces_route
import awf.service.workspaces_retry as workspace_service
from awf.common.config import Settings, get_settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.planning import PLAN_CONFORMANCE_UNSATISFIED

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")

_PROVIDER_AUTH_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CURSOR_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    "OLLAMA_API_KEY",
)
_PROVIDER_READINESS_OVERRIDE_REASON = "unit test fixture supplies provider readiness"
_RETRY_PROVIDER_READINESS_OVERRIDE_PARAMS = {
    "provider_readiness_override": "true",
    "provider_readiness_override_reason": _PROVIDER_READINESS_OVERRIDE_REASON,
}
_V2_RETRY_BODY = {
    "repo": {
        "url": "git@github.com:example/retry-api.git",
        "base_branch": "development",
    },
    "task": {
        "title": "Retry API task",
        "prompt": "Retry this failed workspace.",
        "kind": "feature_branch_pr",
        "agent": "codex",
        "external_id": "TICKET-API-RETRY",
        "task_class": "test_task",
        "owned_paths": ["src/awf/api/retry.py"],
        "auto_merge": False,
    },
    "workspace": {"profile_ref": "python", "profile": None},
    "validation": {"commands": ["pytest tests/unit/api -q"], "requested_tier": 2},
    "resources": {},
    "preflight": {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": _PROVIDER_READINESS_OVERRIDE_REASON,
    },
}


@pytest.fixture(autouse=True)
def _clear_provider_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROVIDER_AUTH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def api_auth_headers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    get_settings.cache_clear()
    yield {"Authorization": "Bearer secret"}
    get_settings.cache_clear()


async def _create_failed_workspace(client: AsyncClient, engine: AsyncEngine) -> str:
    created = await client.post("/v1/workspaces", json=_V2_RETRY_BODY)
    assert created.status_code == 202
    workspace_id = str(created.json()["workspace_id"])

    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = "validation_failure"
        workspace.failure_message = "pytest failed"
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await session.commit()
    return workspace_id


async def _create_cancelled_workspace(client: AsyncClient, engine: AsyncEngine) -> str:
    created = await client.post(
        "/v1/workspaces",
        json={
            **_V2_RETRY_BODY,
            "task": {
                **_V2_RETRY_BODY["task"],
                "external_id": "TICKET-API-RETRY-CANCELLED",
                "owned_paths": ["src/awf/api/retry-cancelled.py"],
            },
        },
    )
    assert created.status_code == 202
    workspace_id = str(created.json()["workspace_id"])

    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCEL")
        await session.commit()
    return workspace_id


async def _create_recovering_workspace(
    client: AsyncClient,
    engine: AsyncEngine,
    *,
    not_before: str = "2026-06-21T12:30:00+00:00",
) -> str:
    created = await client.post(
        "/v1/workspaces",
        json={
            **_V2_RETRY_BODY,
            "task": {
                **_V2_RETRY_BODY["task"],
                "external_id": "TICKET-API-RETRY-RECOVERING",
                "owned_paths": ["src/awf/api/retry-recovering.py"],
            },
        },
    )
    assert created.status_code == 202
    workspace_id = str(created.json()["workspace_id"])

    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        for status in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.recovering,
        ):
            await repo.transition(workspace, to=status, reason_code="TEST")
        workspace.task_policy = {
            **workspace.task_policy,
            "provider_recovery_state": {"not_before": not_before},
        }
        await session.commit()
    return workspace_id


async def _create_conformance_failed_workspace(
    client: AsyncClient,
    engine: AsyncEngine,
) -> str:
    created = await client.post("/v1/workspaces", json=_V2_RETRY_BODY)
    assert created.status_code == 202
    workspace_id = str(created.json()["workspace_id"])

    factory = make_session_factory(engine)
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
                "details": {
                    "conformance": {
                        "summary": "Missing planned test coverage.",
                        "gaps": ["Add retry API regression test"],
                        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                        "iterations_used": 0,
                        "max_iterations": 0,
                        "plan_path": "docs/awf-plans/ws_old.md",
                        "report_path": "docs/awf-plans/ws_old.conformance.json",
                    }
                }
            },
        )
        await session.commit()
    return workspace_id


@pytest.mark.unit
async def test_retry_endpoint_creates_new_requested_workspace(
    client: AsyncClient,
    engine: AsyncEngine,
    api_auth_headers: dict[str, str],
) -> None:
    original_id = await _create_failed_workspace(client, engine)

    response = await client.post(
        f"/v1/workspaces/{original_id}/retry",
        params=_RETRY_PROVIDER_READINESS_OVERRIDE_PARAMS,
        headers=api_auth_headers,
    )

    assert response.status_code == 202
    body = response.json()
    assert body["source_workspace_id"] == original_id
    assert body["new_workspace_id"].startswith("ws_")
    assert body["new_workspace_id"] != original_id
    assert body["operation_id"].startswith("op_")
    assert body["status"] == "requested"
    assert body["attempt_number"] == 2
    assert body["status_url"] == f"/v1/workspaces/{body['new_workspace_id']}"
    assert body["events_url"] == f"/v1/workspaces/{body['new_workspace_id']}/events"

    operations = await client.get(
        f"/v1/workspaces/{body['new_workspace_id']}/operations?type=retry",
        headers=api_auth_headers,
    )
    assert operations.status_code == 200
    assert [item["id"] for item in operations.json()["items"]] == [body["operation_id"]]

    retried = await client.get(
        f"/v1/workspaces/{body['new_workspace_id']}",
        headers=api_auth_headers,
    )
    assert retried.status_code == 200
    retried_body = retried.json()
    assert retried_body["repo_url"] == _V2_RETRY_BODY["repo"]["url"]
    assert retried_body["branch_base"] == _V2_RETRY_BODY["repo"]["base_branch"]
    assert retried_body["task_title"] == _V2_RETRY_BODY["task"]["title"]
    assert retried_body["task_prompt"] == _V2_RETRY_BODY["task"]["prompt"]
    assert retried_body["task_external_id"] == _V2_RETRY_BODY["task"]["external_id"]
    assert retried_body["task_class"] == _V2_RETRY_BODY["task"]["task_class"]
    assert retried_body["owned_paths"] == _V2_RETRY_BODY["task"]["owned_paths"]
    assert retried_body["auto_merge"] is False
    assert retried_body["profile_ref"] == "python"
    assert retried_body["test_commands"] == _V2_RETRY_BODY["validation"]["commands"]
    assert retried_body["failure_reason"] is None
    assert retried_body["failure_message"] is None


@pytest.mark.unit
async def test_retry_endpoint_requires_authorization_when_api_token_configured(
    client: AsyncClient,
    engine: AsyncEngine,
    api_auth_headers: dict[str, str],
) -> None:
    _ = api_auth_headers
    original_id = await _create_failed_workspace(client, engine)

    response = await client.post(
        f"/v1/workspaces/{original_id}/retry",
        params=_RETRY_PROVIDER_READINESS_OVERRIDE_PARAMS,
        headers={"Authorization": "Bearer wrong"},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_retry_endpoint_reports_unrecoverable_conformance_salvage_failure(
    client: AsyncClient,
    engine: AsyncEngine,
    api_auth_headers: dict[str, str],
) -> None:
    original_id = await _create_conformance_failed_workspace(client, engine)

    response = await client.post(
        f"/v1/workspaces/{original_id}/retry",
        params=_RETRY_PROVIDER_READINESS_OVERRIDE_PARAMS,
        headers=api_auth_headers,
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "WORKSPACE_RETRY_SALVAGE_UNAVAILABLE"
    assert body["detail"]["source_workspace_id"] == original_id
    assert body["detail"]["reason_code"] == "SALVAGE_BASE_UNAVAILABLE"
    assert body["detail"]["source_reason_code"] == PLAN_CONFORMANCE_UNSATISFIED
    assert body["detail"]["gaps"] == ["Add retry API regression test"]
    assert body["detail"]["plan_path"] == "docs/awf-plans/ws_old.md"
    assert body["detail"]["report_path"] == "docs/awf-plans/ws_old.conformance.json"


@pytest.mark.unit
async def test_retry_route_direct_success_returns_retry_response(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    original_id = await _create_failed_workspace(client, engine)

    factory = make_session_factory(engine)
    async with factory() as session:
        response = await workspaces_route.retry_workspace(
            original_id,
            provider_readiness_override=True,
            provider_readiness_override_reason=_PROVIDER_READINESS_OVERRIDE_REASON,
            session=session,
        )

    assert response.source_workspace_id == original_id
    assert response.new_workspace_id.startswith("ws_")
    assert response.operation_id.startswith("op_")
    assert response.status == WorkspaceStatus.requested
    assert response.attempt_number == 2


@pytest.mark.unit
async def test_retry_endpoint_accepts_cancelled_workspace(
    client: AsyncClient,
    engine: AsyncEngine,
    api_auth_headers: dict[str, str],
) -> None:
    original_id = await _create_cancelled_workspace(client, engine)

    response = await client.post(
        f"/v1/workspaces/{original_id}/retry",
        params=_RETRY_PROVIDER_READINESS_OVERRIDE_PARAMS,
        headers=api_auth_headers,
    )

    assert response.status_code == 202
    assert response.json()["source_workspace_id"] == original_id
    assert response.json()["status"] == "requested"
    assert response.json()["attempt_number"] == 2


@pytest.mark.unit
async def test_retry_endpoint_rejects_missing_workspace(
    client: AsyncClient,
    api_auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/v1/workspaces/ws_missing_retry/retry",
        headers=api_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "WORKSPACE_NOT_FOUND"


@pytest.mark.unit
async def test_retry_endpoint_rejects_non_terminal_workspace(
    client: AsyncClient,
    api_auth_headers: dict[str, str],
) -> None:
    created = await client.post("/v1/workspaces", json=_V2_RETRY_BODY)
    assert created.status_code == 202
    workspace_id = str(created.json()["workspace_id"])

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/retry",
        headers=api_auth_headers,
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "WORKSPACE_NOT_RETRYABLE"
    assert body["detail"]["status"] == "requested"
    assert body["detail"]["retryable_statuses"] == ["failed", "cancelled"]


@pytest.mark.unit
async def test_retry_endpoint_dedupes_recovering_workspace(
    client: AsyncClient,
    engine: AsyncEngine,
    api_auth_headers: dict[str, str],
) -> None:
    not_before = "2026-06-21T12:30:00+00:00"
    original_id = await _create_recovering_workspace(client, engine, not_before=not_before)

    response = await client.post(
        f"/v1/workspaces/{original_id}/retry",
        params=_RETRY_PROVIDER_READINESS_OVERRIDE_PARAMS,
        headers=api_auth_headers,
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "WORKSPACE_AUTO_RETRY_IN_FLIGHT"
    assert "cooldown" in body["message"]
    assert body["detail"] == {
        "status": "recovering",
        "provider_cooldown_not_before": not_before,
        "reason": "auto_retry_in_flight",
    }

    # The colliding manual retry must not spawn a duplicate workspace.
    overview = await client.get("/v1/workspaces/overview", headers=api_auth_headers)
    assert overview.status_code == 200
    assert [item["workspace_id"] for item in overview.json()["items"]] == [original_id]


@pytest.mark.unit
async def test_retry_endpoint_blocks_missing_provider_readiness(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    api_auth_headers: dict[str, str],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    original_id = await _create_failed_workspace(client, engine)
    monkeypatch.setattr(
        workspace_service,
        "get_settings",
        lambda: Settings(_env_file=None, host_home=str(tmp_path / "home"), docker_host=""),
    )

    response = await client.post(
        f"/v1/workspaces/{original_id}/retry",
        headers=api_auth_headers,
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "PROVIDER_READINESS_PRECHECK_FAILED"
    preflight = body["detail"]["provider_readiness_preflight"]
    assert preflight["provider"] == "codex"
    assert preflight["model"] == "gpt-5.5"
    assert preflight["blocks_launch"] is True


@pytest.mark.unit
async def test_retry_endpoint_override_records_preflight(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    api_auth_headers: dict[str, str],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    original_id = await _create_failed_workspace(client, engine)
    monkeypatch.setattr(
        workspace_service,
        "get_settings",
        lambda: Settings(_env_file=None, host_home=str(tmp_path / "home"), docker_host=""),
    )

    response = await client.post(
        f"/v1/workspaces/{original_id}/retry",
        params={
            "provider_readiness_override": "true",
            "provider_readiness_override_reason": "operator verified local auth",
        },
        headers=api_auth_headers,
    )

    assert response.status_code == 202
    body = response.json()
    preflight = body["provider_readiness_preflight"]
    assert preflight["source_workspace_id"] == original_id
    assert preflight["override_used"] is True
    assert preflight["override_reason"] == "operator verified local auth"
