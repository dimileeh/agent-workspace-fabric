"""MCP server + tool behaviour tests.

We exercise the tools via ``mcp.call_tool(name, args)`` (FastMCP's in-process
harness) against a throwaway PostgreSQL. This validates:
- All tools are registered under the expected names.
- Each tool's happy path returns the same payload shape as the REST API.
- wait_for_workspace exits on terminal state without hanging.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import (
    OperationResponse,
    WorkspaceControlResponse,
)
from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    SecretLeaseIssue,
    SecretLeaseRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.service.controls import WorkspaceControlError
from awf.service.disk import DiskCheck
from tests.postgres import postgres_test_engine

_PROVIDER_AUTH_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
    "CURSOR_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def mcp(factory: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    service = WorkspaceService(factory)
    return build_mcp_server(service=service)


_CREATE_ARGS: dict[str, object] = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "base_branch": "development",
    "task_title": "Add docstring",
    "task_prompt": "Add a one-line docstring to src/module/__init__.py.",
    "agent": "codex",
    "validation_commands": ["pytest -q"],
    "provider_readiness_override": True,
    "provider_readiness_override_reason": "mcp default create fixture",
}


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


def _low_disk_check(settings: Settings) -> DiskCheck:
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=100,
        used_bytes=95,
        free_bytes=5,
        percent_free=5.0,
        threshold_bytes=10,
        ok=False,
        status="fail",
        reason="INSUFFICIENT_DISK",
        detail="free_bytes=5 threshold_bytes=10",
    )


def _ok_disk_check(settings: Settings) -> DiskCheck:
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=100,
        used_bytes=20,
        free_bytes=80,
        percent_free=80.0,
        threshold_bytes=10,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
        detail=None,
    )


async def _call(mcp, name, args) -> object:  # type: ignore[no-untyped-def]
    """Unwrap FastMCP's call_tool payload.

    FastMCP returns ``(content, structured)`` where ``structured`` is the
    tool's return value for dict returns, or ``{"result": <value>}`` for
    primitive / None / list returns. This helper normalises to the underlying
    value so tests can assert against it directly.
    """
    result = await mcp.call_tool(name, args)
    if isinstance(result, CallToolResult):
        return result.structuredContent
    _, payload = result
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


def _workspace_id(payload: object) -> str:
    assert isinstance(payload, dict)
    return str(payload["workspace_id"])


def _optional_string_schema(schema: dict[str, object]) -> dict[str, object]:
    any_of = schema.get("anyOf")
    assert isinstance(any_of, list)
    string_schema = next(
        (item for item in any_of if isinstance(item, dict) and item.get("type") == "string"),
        None,
    )
    assert string_schema is not None, f"Could not find string schema in anyOf: {any_of}"
    assert isinstance(string_schema, dict)
    return string_schema


def _optional_object_schema(schema: dict[str, object]) -> dict[str, object]:
    any_of = schema.get("anyOf")
    if any_of is None:
        assert schema.get("type") == "object"
        return schema

    assert isinstance(any_of, list)
    object_schema = next(
        (item for item in any_of if isinstance(item, dict) and item.get("type") == "object"),
        None,
    )
    assert object_schema is not None, f"Could not find object schema in anyOf: {any_of}"
    assert isinstance(object_schema, dict)
    return object_schema


def _assert_idempotency_key_schema(schema: dict[str, object]) -> None:
    string_schema = _optional_string_schema(schema)
    assert str(schema["description"]).startswith("Required idempotency key")
    assert schema["minLength"] == 1
    assert string_schema["maxLength"] == 128
    assert "default" not in schema


class _RecordingControlService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        stop_stack: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "cancel",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "stop_stack": stop_stack,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id="op_cancel",
            operation_status="succeeded",
            status="cancelled",
            message="workspace cancellation requested",
        )

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "stop",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id="op_stop",
            operation_status="succeeded",
            status="cancelled",
            message="workspace stack stopped",
        )

    async def destroy_workspace(
        self,
        workspace_id: str,
        *,
        force: bool,
        remove_volumes: bool,
        remove_worktree: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        self.calls.append(
            (
                "destroy",
                {
                    "workspace_id": workspace_id,
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "idempotency_key": idempotency_key,
                    "expected_version": expected_version,
                },
            )
        )
        return WorkspaceControlResponse(
            workspace_id=workspace_id,
            operation_id="op_destroy",
            operation_status="succeeded",
            status="destroyed",
            message="workspace destroyed",
        )


class _FailingControlService(_RecordingControlService):
    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        stop_stack: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        del workspace_id, reason, stop_stack, idempotency_key, expected_version
        raise WorkspaceControlError(error_code="NOPE", message="cancel refused")

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        del workspace_id, reason, idempotency_key, expected_version
        raise WorkspaceControlError(error_code="NOPE", message="stop refused")


class TestGetAndList:
    @pytest.mark.unit
    async def test_get_returns_the_workspace_just_created(self, mcp) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)

        fetched = await _call(mcp, "awf_get_workspace", {"workspace_id": ws_id})
        assert fetched is not None
        assert fetched["id"] == ws_id  # type: ignore[index]
        assert fetched["status"] == "requested"  # type: ignore[index]

    @pytest.mark.unit
    async def test_get_workspace_includes_issued_secret_leases(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)
        raw_ref = "sk-live-do-not-appear-in-mcp"
        now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(ws_id)
            assert workspace is not None
            await SecretLeaseRepository(session).issue_declared_leases(
                workspace,
                leases=[
                    SecretLeaseIssue(
                        secret_name="api-token",
                        kind="env",
                        target="API_TOKEN",
                        mode="ro",
                        required=True,
                        provider="vault",
                        ref_digest="sha256:" + "8" * 64,
                        expires_at=now + timedelta(hours=1),
                        issue_metadata={
                            "profile": "api",
                            "declaration_index": 0,
                            "raw_ref": raw_ref,
                        },
                    )
                ],
                now=now,
            )
            await session.commit()

        fetched = await _call(mcp, "awf_get_workspace", {"workspace_id": ws_id})

        assert isinstance(fetched, dict)
        assert fetched["secret_leases"][0]["secret_name"] == "api-token"
        assert fetched["secret_leases"][0]["status"] == "issued"
        assert fetched["secret_leases"][0]["ref_digest"] == "sha256:" + "8" * 64
        assert raw_ref not in json.dumps(fetched)

    @pytest.mark.unit
    async def test_get_unknown_id_returns_none(self, mcp) -> None:  # type: ignore[no-untyped-def]
        result = await _call(mcp, "awf_get_workspace", {"workspace_id": "ws_nope"})
        assert result is None

    @pytest.mark.unit
    async def test_list_returns_newest_first(self, mcp) -> None:  # type: ignore[no-untyped-def]
        ids: list[str] = []
        for title in ["first", "second", "third"]:
            args = {**_CREATE_ARGS, "task_title": title}
            created = await _call(mcp, "awf_create_workspace", args)
            ids.append(_workspace_id(created))

        listed = await _call(mcp, "awf_list_workspaces", {"limit": 10})
        assert isinstance(listed, list)
        assert [r["id"] for r in listed] == list(reversed(ids))

    @pytest.mark.unit
    async def test_list_filters_by_status_agent_and_repo_url(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        repo_url = "git@github.com:example/filtered.git"
        matching = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": repo_url,
                "task_title": "matching",
                "agent": "opencode",
            },
        )
        wrong_status = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": repo_url,
                "task_title": "wrong status",
                "agent": "opencode",
            },
        )
        wrong_agent = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": repo_url,
                "task_title": "wrong agent",
                "agent": "codex",
            },
        )
        wrong_repo = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": "git@github.com:example/other.git",
                "task_title": "wrong repo",
                "agent": "opencode",
            },
        )
        assert isinstance(matching, dict)
        assert isinstance(wrong_status, dict)
        assert isinstance(wrong_agent, dict)
        assert isinstance(wrong_repo, dict)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            for workspace_id in (
                _workspace_id(matching),
                _workspace_id(wrong_agent),
                _workspace_id(wrong_repo),
            ):
                workspace = await repo.get(str(workspace_id))
                assert workspace is not None
                await repo.transition(
                    workspace,
                    to=WorkspaceStatus.provisioning,
                    reason_code="TEST",
                )
                await repo.transition(workspace, to=WorkspaceStatus.ready, reason_code="TEST")
            await session.commit()

        listed = await _call(
            mcp,
            "awf_list_workspaces",
            {
                "status": "ready",
                "agent": "opencode",
                "repo_url": repo_url,
                "limit": 10,
            },
        )

        assert isinstance(listed, list)
        assert [row["id"] for row in listed] == [_workspace_id(matching)]
        assert _workspace_id(wrong_status) not in [row["id"] for row in listed]
