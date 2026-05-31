"""MCP server + tool behaviour tests.

We exercise the tools via ``mcp.call_tool(name, args)`` (FastMCP's in-process
harness) against a throwaway PostgreSQL. This validates:
- All tools are registered under the expected names.
- Each tool's happy path returns the same payload shape as the REST API.
- wait_for_workspace exits on terminal state without hanging.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import (
    OperationResponse,
    PullRequestMonitorAdoptionResponse,
    WorkspaceControlResponse,
)
from awf.common.config import Settings
from awf.common.github_client import PullRequestAdoptionMetadata, RepoRef
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.mcp import server as mcp_server
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.service.controls import WorkspaceControlError
from awf.service.disk import DiskCheck
from awf.service.workspaces import OperationRowsPage, WorkspaceRetryError
from tests.postgres import postgres_test_engine

_PROVIDER_AUTH_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
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


def _optional_array_schema(schema: dict[str, object]) -> dict[str, object]:
    any_of = schema.get("anyOf")
    if any_of is None:
        assert schema.get("type") == "array"
        return schema

    assert isinstance(any_of, list)
    array_schema = next(
        (item for item in any_of if isinstance(item, dict) and item.get("type") == "array"),
        None,
    )
    assert array_schema is not None, f"Could not find array schema in anyOf: {any_of}"
    assert isinstance(array_schema, dict)
    return array_schema


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


@pytest.mark.unit
async def test_build_mcp_server_captures_default_settings_once(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(_env_file=None, work_dir=str(tmp_path / "awf-state"))
    calls = 0

    def fake_get_settings() -> Settings:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("MCP tools should reuse settings captured at build time")
        return settings

    monkeypatch.setattr(mcp_server, "get_settings", fake_get_settings)
    mcp = build_mcp_server(service=WorkspaceService(factory))

    assert calls == 1

    for _ in range(2):
        result = await mcp.call_tool(
            "awf_list_workspace_artifacts",
            {"workspace_id": "ws_missing"},
        )
        assert isinstance(result, CallToolResult)
        assert result.structuredContent is None

    assert calls == 1


@pytest.mark.unit
def test_workspace_retry_error_result_uses_structured_error_payload() -> None:
    result = mcp_server._workspace_retry_error_result(
        WorkspaceRetryError("retry refused", detail={"workspace_id": "ws_x"})
    )

    assert result.isError is True
    assert result.structuredContent == {
        "error_code": "WORKSPACE_RETRY_ERROR",
        "message": "retry refused",
        "detail": {"workspace_id": "ws_x"},
    }


@pytest.mark.unit
async def test_core_release_readiness_rejects_invalid_provider_names(mcp) -> None:  # type: ignore[no-untyped-def]
    result = await mcp.call_tool(
        "awf_get_core_release_readiness",
        {"providers": ["bogus-provider"]},
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == "INVALID_PROVIDERS"


class TestToolRegistration:
    @pytest.mark.unit
    async def test_existing_and_observability_tools_registered(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert {
            "awf_create_workspace",
            "awf_get_workspace",
            "awf_list_workspaces",
            "awf_wait_for_workspace",
            "awf_adopt_pull_request_monitor",
        } <= names
        assert {
            "awf_create_workspace",
            "awf_get_workspace_runtime",
            "awf_list_workspace_operations",
            "awf_list_workspace_events",
            "awf_list_workspace_logs",
            "awf_read_workspace_log",
        } <= names
        assert {
            "awf_cancel_workspace",
            "awf_stop_workspace",
            "awf_destroy_workspace",
        } <= names
        assert {
            "awf_list_merge_queue",
            "awf_list_workspace_overview",
            "awf_list_workspace_validation",
            "awf_list_workspace_stale_reasons",
            "awf_list_workspace_artifacts",
            "awf_read_workspace_artifact",
            "awf_get_failure_analysis_summary",
            "awf_get_workspace_reliability_summary",
            "awf_get_resource_saturation_summary",
            "awf_get_slo_metrics_summary",
            "awf_get_core_release_readiness",
            "awf_list_operations",
            "awf_get_operation",
            "awf_get_overlap_graph",
            "awf_list_tasks",
            "awf_list_task_attempts",
            "awf_list_locks",
            "awf_get_service_readiness",
            "awf_get_service_health",
            "awf_list_events",
        } <= names
        # Covered by the block above (which is a superset including awf_list_events)

    @pytest.mark.unit
    async def test_control_tools_are_described_as_operator_controls(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        tools = {tool.name: tool for tool in await mcp.list_tools()}

        for name in (
            "awf_cancel_workspace",
            "awf_stop_workspace",
            "awf_destroy_workspace",
            "awf_remonitor_workspace",
            "awf_request_workspace_validation",
        ):
            description = (tools[name].description or "").lower()
            assert "operator control" in description
            assert "not shell access" in description

    @pytest.mark.unit
    async def test_control_tool_argument_contracts(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = {tool.name: tool for tool in await mcp.list_tools()}

        cancel_props = tools["awf_cancel_workspace"].inputSchema["properties"]
        cancel_required = tools["awf_cancel_workspace"].inputSchema.get("required", [])
        assert cancel_props["reason"]["default"] is None
        assert cancel_props["stop_stack"]["default"] is True
        assert "idempotency_key" in cancel_required
        _assert_idempotency_key_schema(cancel_props["idempotency_key"])
        assert cancel_props["expected_version"]["default"] is None
        assert "expected_version" not in cancel_required

        stop_props = tools["awf_stop_workspace"].inputSchema["properties"]
        stop_required = tools["awf_stop_workspace"].inputSchema.get("required", [])
        assert stop_props["reason"]["default"] is None
        assert "stop_stack" not in stop_props
        assert "idempotency_key" in stop_required
        _assert_idempotency_key_schema(stop_props["idempotency_key"])
        assert stop_props["expected_version"]["default"] is None
        assert "expected_version" not in stop_required

        destroy_props = tools["awf_destroy_workspace"].inputSchema["properties"]
        destroy_required = tools["awf_destroy_workspace"].inputSchema.get("required", [])
        assert destroy_props["force"]["default"] is False
        assert destroy_props["remove_volumes"]["default"] is True
        assert destroy_props["remove_worktree"]["default"] is True
        assert "idempotency_key" in destroy_required
        _assert_idempotency_key_schema(destroy_props["idempotency_key"])
        assert destroy_props["expected_version"]["default"] is None
        assert "expected_version" not in destroy_required

        remonitor_props = tools["awf_remonitor_workspace"].inputSchema["properties"]
        remonitor_required = tools["awf_remonitor_workspace"].inputSchema.get("required", [])
        assert "idempotency_key" in remonitor_required
        _assert_idempotency_key_schema(remonitor_props["idempotency_key"])
        assert remonitor_props["expected_version"]["default"] is None
        assert "expected_version" not in remonitor_required

        validate_props = tools["awf_request_workspace_validation"].inputSchema["properties"]
        validate_required = tools["awf_request_workspace_validation"].inputSchema.get(
            "required", []
        )
        assert "idempotency_key" in validate_required
        _assert_idempotency_key_schema(validate_props["idempotency_key"])
        assert validate_props["expected_version"]["default"] is None
        assert "expected_version" not in validate_required

        refresh_props = tools["awf_refresh_workspace"].inputSchema["properties"]
        assert "idempotency_key" in refresh_props
        refresh_required = tools["awf_refresh_workspace"].inputSchema.get("required", [])
        assert "idempotency_key" in refresh_required
        _assert_idempotency_key_schema(refresh_props["idempotency_key"])
        assert refresh_props["expected_version"]["default"] is None
        assert "expected_version" not in refresh_required

        rebase_props = tools["awf_rebase_workspace"].inputSchema["properties"]
        assert "idempotency_key" in rebase_props
        rebase_required = tools["awf_rebase_workspace"].inputSchema.get("required", [])
        assert "idempotency_key" in rebase_required
        _assert_idempotency_key_schema(rebase_props["idempotency_key"])
        assert rebase_props["expected_version"]["default"] is None
        assert "expected_version" not in rebase_required

    @pytest.mark.unit
    async def test_create_workspace_owned_paths_declares_item_constraints(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = await mcp.list_tools()
        create = next(tool for tool in tools if tool.name == "awf_create_workspace")
        base_branch = create.inputSchema["properties"]["base_branch"]
        env_profile = create.inputSchema["properties"]["env_profile"]
        owned_paths = create.inputSchema["properties"]["owned_paths"]
        out_of_scope_changes = create.inputSchema["properties"]["out_of_scope_changes"]
        provider_recovery = create.inputSchema["properties"]["provider_recovery"]
        companions = create.inputSchema["properties"]["companions"]

        assert base_branch["default"] == "development"
        assert "Defaults to development" in base_branch["description"]
        assert env_profile["default"] is None
        assert _optional_string_schema(env_profile)["maxLength"] == 128
        assert "Legacy alias for profile_ref" in env_profile["description"]
        assert owned_paths["maxItems"] == 128
        assert owned_paths["items"] == {
            "maxLength": 512,
            "minLength": 1,
            "type": "string",
        }
        assert _optional_object_schema(out_of_scope_changes)["type"] == "object"
        assert _optional_object_schema(provider_recovery)["type"] == "object"
        companion_schema = _optional_array_schema(companions)
        assert companion_schema["maxItems"] == 16
        assert companion_schema["items"]["type"] == "object"
        assert create.inputSchema["properties"]["provider_readiness_override"]["default"] is False
        assert (
            create.inputSchema["properties"]["provider_readiness_override_reason"]["default"]
            is None
        )

    @pytest.mark.unit
    async def test_adopt_pull_request_monitor_tool_creates_adoption(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async def _fetcher(
            *,
            repo: RepoRef,
            pr_number: int,
        ) -> PullRequestAdoptionMetadata:
            assert repo.slug() == "dimileeh/aira-web"
            assert pr_number == 277
            return PullRequestAdoptionMetadata(
                number=277,
                head_ref="feature/ready",
                head_repo_slug="dimileeh/aira-web",
                base_ref="development",
                head_sha="h" * 40,
                base_sha="b" * 40,
                state="OPEN",
                is_draft=False,
                closed=False,
                merged=False,
                author="octocat",
                url="https://github.com/dimileeh/aira-web/pull/277",
                title="feature: ready",
            )

        mcp = build_mcp_server(
            service=WorkspaceService(factory, pr_adoption_metadata_fetcher=_fetcher)
        )

        payload = await _call(
            mcp,
            "awf_adopt_pull_request_monitor",
            {
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "auto_merge": False,
                "model": "gpt-5.3-codex",
            },
        )

        assert isinstance(payload, dict)
        assert payload["workspace_id"].startswith("ws_")
        assert payload["repo_slug"] == "dimileeh/aira-web"
        assert payload["pr_number"] == 277
        assert payload["head_ref"] == "feature/ready"
        assert payload["auto_merge"] is False
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(str(payload["workspace_id"]))
        assert workspace is not None
        assert workspace.task_policy["agent_model"] == "gpt-5.3-codex"
        assert workspace.task_policy["agent_effort"] == "xhigh"

    @pytest.mark.unit
    async def test_adopt_pull_request_monitor_tool_forwards_model_and_effort(self) -> None:
        class _CaptureService:
            def __init__(self) -> None:
                self.request = None

            async def adopt_pull_request_monitor(self, request):  # type: ignore[no-untyped-def]
                self.request = request
                return PullRequestMonitorAdoptionResponse(
                    workspace_id="ws_adopt",
                    status=WorkspaceStatus.requested,
                    version=1,
                    repo_slug="dimileeh/aira-web",
                    repo_url="git@github.com:dimileeh/aira-web.git",
                    pr_number=277,
                    pr_url="https://github.com/dimileeh/aira-web/pull/277",
                    head_ref="feature/ready",
                    base_ref="development",
                    auto_merge=True,
                    attached_existing=False,
                    status_url="/v1/workspaces/ws_adopt",
                    events_url="/v1/workspaces/ws_adopt/events",
                    logs_url="/v1/workspaces/ws_adopt/logs",
                )

        service = _CaptureService()
        mcp = build_mcp_server(service=service)  # type: ignore[arg-type]

        payload = await _call(
            mcp,
            "awf_adopt_pull_request_monitor",
            {
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "model": "gpt-5.3-codex",
                "effort": "high",
                "owned_paths": [".github/workflows/publish.yml", "pyproject.toml"],
            },
        )

        assert isinstance(payload, dict)
        assert payload["workspace_id"] == "ws_adopt"
        assert service.request is not None
        assert service.request.model == "gpt-5.3-codex"
        assert service.request.effort == "high"
        assert service.request.owned_paths == [
            ".github/workflows/publish.yml",
            "pyproject.toml",
        ]

    @pytest.mark.unit
    async def test_adopt_pull_request_monitor_tool_ignores_destroyed_prior_adoption(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async def _fetcher(
            *,
            repo: RepoRef,
            pr_number: int,
        ) -> PullRequestAdoptionMetadata:
            assert repo.slug() == "dimileeh/aira-web"
            assert pr_number == 277
            return PullRequestAdoptionMetadata(
                number=277,
                head_ref="feature/ready",
                head_repo_slug="dimileeh/aira-web",
                base_ref="development",
                head_sha="h" * 40,
                base_sha="b" * 40,
                state="OPEN",
                is_draft=False,
                closed=False,
                merged=False,
                author="octocat",
                url="https://github.com/dimileeh/aira-web/pull/277",
                title="feature: ready",
            )

        mcp = build_mcp_server(
            service=WorkspaceService(factory, pr_adoption_metadata_fetcher=_fetcher)
        )
        first = await _call(
            mcp,
            "awf_adopt_pull_request_monitor",
            {"repo_slug": "dimileeh/aira-web", "pr_number": 277},
        )
        assert isinstance(first, dict)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(str(first["workspace_id"]))
            assert workspace is not None
            workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        second = await _call(
            mcp,
            "awf_adopt_pull_request_monitor",
            {"repo_slug": "dimileeh/aira-web", "pr_number": 277},
        )

        assert isinstance(second, dict)
        assert second["attached_existing"] is False
        assert second["workspace_id"] != first["workspace_id"]
        assert second["status"] == "requested"

    @pytest.mark.unit
    async def test_adopt_pull_request_monitor_tool_returns_policy_conflict_error_result(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async def _fetcher(
            *,
            repo: RepoRef,
            pr_number: int,
        ) -> PullRequestAdoptionMetadata:
            assert repo.slug() == "dimileeh/aira-web"
            assert pr_number == 277
            return PullRequestAdoptionMetadata(
                number=277,
                head_ref="feature/ready",
                head_repo_slug="dimileeh/aira-web",
                base_ref="development",
                head_sha="h" * 40,
                base_sha="b" * 40,
                state="OPEN",
                is_draft=False,
                closed=False,
                merged=False,
                author="octocat",
                url="https://github.com/dimileeh/aira-web/pull/277",
                title="feature: ready",
            )

        mcp = build_mcp_server(
            service=WorkspaceService(factory, pr_adoption_metadata_fetcher=_fetcher)
        )
        first = await _call(
            mcp,
            "awf_adopt_pull_request_monitor",
            {
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "auto_merge": False,
            },
        )
        assert isinstance(first, dict)

        result = await mcp.call_tool(
            "awf_adopt_pull_request_monitor",
            {
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
                "auto_merge": True,
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "PR_ADOPTION_POLICY_CONFLICT"
        assert result.structuredContent["detail"] == {
            "workspace_id": first["workspace_id"],
            "existing_auto_merge": False,
            "requested_auto_merge": True,
        }

    @pytest.mark.unit
    async def test_adopt_pull_request_monitor_tool_returns_terminal_pr_error_result(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async def _fetcher(
            *,
            repo: RepoRef,
            pr_number: int,
        ) -> PullRequestAdoptionMetadata:
            assert repo.slug() == "dimileeh/aira-web"
            assert pr_number == 277
            return PullRequestAdoptionMetadata(
                number=277,
                head_ref="feature/ready",
                head_repo_slug="dimileeh/aira-web",
                base_ref="development",
                head_sha="h" * 40,
                base_sha="b" * 40,
                state="MERGED",
                is_draft=False,
                closed=True,
                merged=True,
                author="octocat",
                url="https://github.com/dimileeh/aira-web/pull/277",
                title="feature: ready",
            )

        mcp = build_mcp_server(
            service=WorkspaceService(factory, pr_adoption_metadata_fetcher=_fetcher)
        )

        result = await mcp.call_tool(
            "awf_adopt_pull_request_monitor",
            {"repo_slug": "dimileeh/aira-web", "pr_number": 277},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "PR_ALREADY_MERGED"
        assert "already merged" in result.structuredContent["message"]

    @pytest.mark.unit
    async def test_operator_parity_tool_argument_contracts(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = {tool.name: tool for tool in await mcp.list_tools()}

        list_workspaces_props = tools["awf_list_workspaces"].inputSchema["properties"]
        assert list_workspaces_props["limit"]["default"] == 50
        assert list_workspaces_props["limit"]["minimum"] == 1
        assert list_workspaces_props["limit"]["maximum"] == 500
        assert "status" in list_workspaces_props
        assert list_workspaces_props["status"]["default"] is None
        assert "workspace_status" not in list_workspaces_props
        assert "agent" in list_workspaces_props
        assert list_workspaces_props["agent"]["default"] is None
        repo_url_schema = _optional_string_schema(list_workspaces_props["repo_url"])
        assert repo_url_schema["maxLength"] == 512
        assert repo_url_schema["minLength"] == 1

        adopt_props = tools["awf_adopt_pull_request_monitor"].inputSchema["properties"]
        model_schema = _optional_string_schema(adopt_props["model"])
        effort_schema = _optional_string_schema(adopt_props["effort"])
        owned_paths_schema = adopt_props["owned_paths"]
        assert model_schema["maxLength"] == 128
        assert model_schema["minLength"] == 1
        assert effort_schema["maxLength"] == 64
        assert effort_schema["minLength"] == 1
        assert owned_paths_schema["maxItems"] == 128
        assert owned_paths_schema["items"] == {
            "maxLength": 512,
            "minLength": 1,
            "type": "string",
        }

        create_props = tools["awf_create_workspace"].inputSchema["properties"]
        create_model_schema = _optional_string_schema(create_props["model"])
        create_effort_schema = _optional_string_schema(create_props["effort"])
        assert create_model_schema["maxLength"] == 128
        assert create_model_schema["minLength"] == 1
        assert create_effort_schema["maxLength"] == 64
        assert create_effort_schema["minLength"] == 1

        merge_props = tools["awf_list_merge_queue"].inputSchema["properties"]
        repo_url_schema = _optional_string_schema(merge_props["repo_url"])
        assert repo_url_schema["maxLength"] == 512
        assert repo_url_schema["minLength"] == 1
        base_branch_schema = _optional_string_schema(merge_props["base_branch"])
        assert base_branch_schema["maxLength"] == 256
        assert merge_props["limit"]["default"] == 50
        assert merge_props["limit"]["minimum"] == 1
        assert merge_props["limit"]["maximum"] == 500
        assert _optional_string_schema(merge_props["cursor"])["maxLength"] == 128
        assert "status" in merge_props
        assert "workspace_status" not in merge_props

        overview_props = tools["awf_list_workspace_overview"].inputSchema["properties"]
        assert overview_props["limit"]["default"] == 50
        assert _optional_string_schema(overview_props["cursor"])["maxLength"] == 128
        assert "status" in overview_props
        assert "workspace_status" not in overview_props

        operations_props = tools["awf_list_operations"].inputSchema["properties"]
        assert "type" in operations_props
        assert "operation_type" not in operations_props
        assert operations_props["limit"]["default"] == 50
        assert operations_props["limit"]["maximum"] == 500
        assert _optional_string_schema(operations_props["cursor"])["maxLength"] == 128

        workspace_operations_props = tools["awf_list_workspace_operations"].inputSchema[
            "properties"
        ]
        assert "status" in workspace_operations_props
        assert "type" in workspace_operations_props
        assert "operation_type" not in workspace_operations_props
        assert workspace_operations_props["limit"]["default"] == 50
        assert workspace_operations_props["limit"]["maximum"] == 500
        assert _optional_string_schema(workspace_operations_props["cursor"])["maxLength"] == 128

        overlap_props = tools["awf_get_overlap_graph"].inputSchema["properties"]
        assert overlap_props["limit"]["default"] == 100
        assert overlap_props["limit"]["maximum"] == 500

        tasks_props = tools["awf_list_tasks"].inputSchema["properties"]
        assert tasks_props["limit"]["default"] == 50
        assert tasks_props["limit"]["minimum"] == 1
        assert tasks_props["limit"]["maximum"] == 500
        assert "status" in tasks_props
        assert tasks_props["status"]["default"] is None
        repo_url_schema = _optional_string_schema(tasks_props["repo_url"])
        assert repo_url_schema["maxLength"] == 512
        assert repo_url_schema["minLength"] == 1

        task_attempts_props = tools["awf_list_task_attempts"].inputSchema["properties"]
        assert "task_ref" in task_attempts_props
        required_fields = tools["awf_list_task_attempts"].inputSchema.get("required", [])
        assert "task_ref" in required_fields
        assert task_attempts_props["limit"]["default"] == 100
        assert task_attempts_props["limit"]["minimum"] == 1
        assert task_attempts_props["limit"]["maximum"] == 500

        locks_props = tools["awf_list_locks"].inputSchema["properties"]
        assert locks_props["limit"]["default"] == 50
        assert locks_props["limit"]["minimum"] == 1
        assert locks_props["limit"]["maximum"] == 500
        assert "status" in locks_props
        assert "workspace_status" not in locks_props
        cursor_schema = _optional_string_schema(locks_props["cursor"])
        assert cursor_schema["maxLength"] == 256

        events_props = tools["awf_list_events"].inputSchema["properties"]
        assert events_props["limit"]["default"] == 50
        assert events_props["limit"]["minimum"] == 1
        assert events_props["limit"]["maximum"] == 500
        assert "workspace_id" in events_props
        assert events_props["workspace_id"]["default"] is None
        assert "event_type" in events_props
        assert events_props["event_type"]["default"] is None

        workspace_events_props = tools["awf_list_workspace_events"].inputSchema["properties"]
        assert workspace_events_props["limit"]["default"] == 50
        assert workspace_events_props["limit"]["minimum"] == 1
        assert workspace_events_props["limit"]["maximum"] == 500
        assert "cursor" not in workspace_events_props
        readiness_props = tools["awf_get_service_readiness"].inputSchema["properties"]
        assert "limit" not in readiness_props
        assert "providers" in readiness_props
        assert readiness_props["providers"]["default"] is None
        readiness_required = tools["awf_get_service_readiness"].inputSchema.get("required", [])
        assert "providers" not in readiness_required
        assert "limit" not in tools["awf_get_service_health"].inputSchema.get("properties", {})

        retry_props = tools["awf_retry_workspace"].inputSchema["properties"]
        assert "workspace_id" in retry_props
        retry_required = tools["awf_retry_workspace"].inputSchema.get("required", [])
        assert "workspace_id" in retry_required
        assert retry_props["provider_readiness_override"]["default"] is False
        assert retry_props["provider_readiness_override_reason"]["default"] is None

        remonitor_props = tools["awf_remonitor_workspace"].inputSchema["properties"]
        assert "workspace_id" in remonitor_props
        remonitor_required = tools["awf_remonitor_workspace"].inputSchema.get("required", [])
        assert "workspace_id" in remonitor_required
        assert "idempotency_key" in remonitor_required
        assert remonitor_props["reason"]["default"] is None
        assert "idempotency_key" in remonitor_props
        _assert_idempotency_key_schema(remonitor_props["idempotency_key"])

        validate_props = tools["awf_request_workspace_validation"].inputSchema["properties"]
        assert "workspace_id" in validate_props
        validate_required = tools["awf_request_workspace_validation"].inputSchema.get(
            "required", []
        )
        assert "workspace_id" in validate_required
        assert "idempotency_key" in validate_required
        assert validate_props["reason"]["default"] is None
        assert validate_props["requested_tier"]["default"] is None
        assert "idempotency_key" in validate_props
        _assert_idempotency_key_schema(validate_props["idempotency_key"])


class TestOperationTools:
    @pytest.mark.unit
    async def test_list_operations_reports_has_more_when_limit_truncates(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe operations",
                task_prompt="List operations.",
                agent="codex",
                test_commands=[],
            )
            repo = OperationRepository(session)
            create = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.create,
                status=OperationStatus.succeeded,
            )
            validate = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.validate,
                status=OperationStatus.running,
            )
            stop = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.stop,
                status=OperationStatus.pending,
            )
            create.created_at = base
            validate.created_at = base + timedelta(seconds=1)
            stop.created_at = base + timedelta(seconds=2)
            await session.commit()

        payload = await _call(mcp, "awf_list_operations", {"limit": 2})

        assert isinstance(payload, dict)
        assert [item["id"] for item in payload["items"]] == [stop.id, validate.id]
        assert payload["has_more"] is True
        assert payload["next_cursor"] is not None
        assert payload["limit"] == 2
        assert payload["cursor"] is None

        second_page = await _call(
            mcp,
            "awf_list_operations",
            {"limit": 2, "cursor": payload["next_cursor"]},
        )

        assert isinstance(second_page, dict)
        assert [item["id"] for item in second_page["items"]] == [create.id]
        assert second_page["has_more"] is False
        assert second_page["next_cursor"] is None
        assert second_page["cursor"] == payload["next_cursor"]

    @pytest.mark.unit
    async def test_list_operations_uses_prevalidated_service_responses(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        operation = _operation_response()

        class PrevalidatedOperationService:
            async def list_all_operations_page(self, **kwargs: object) -> OperationRowsPage:
                return OperationRowsPage(rows=[operation])

        def fail_model_validate(cls, value) -> OperationResponse:  # type: ignore[no-untyped-def]
            raise AssertionError("OperationResponse.model_validate should not be called")

        monkeypatch.setattr(OperationResponse, "model_validate", classmethod(fail_model_validate))
        mcp = build_mcp_server(service=PrevalidatedOperationService())  # type: ignore[arg-type]

        payload = await _call(mcp, "awf_list_operations", {})

        assert isinstance(payload, dict)
        assert payload["items"][0]["id"] == operation.id

    @pytest.mark.unit
    async def test_list_operations_accepts_rest_type_filter(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Filter global operations",
                task_prompt="List filtered operations.",
                agent="codex",
                test_commands=[],
            )
            repo = OperationRepository(session)
            create = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.create,
                status=OperationStatus.succeeded,
            )
            validate = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.validate,
                status=OperationStatus.running,
            )
            create.created_at = base
            validate.created_at = base + timedelta(seconds=1)
            await session.commit()

        payload = await _call(
            mcp,
            "awf_list_operations",
            {
                "workspace_id": workspace.id,
                "type": "validate",
            },
        )

        assert isinstance(payload, dict)
        assert [item["id"] for item in payload["items"]] == [validate.id]
        assert [item["type"] for item in payload["items"]] == ["validate"]


class TestWorkspaceControls:
    @pytest.mark.unit
    async def test_cancel_workspace_calls_service_and_returns_structured_response(
        self,
    ) -> None:
        service = _RecordingControlService()
        mcp = build_mcp_server(service=service)  # type: ignore[arg-type]

        payload = await _call(
            mcp,
            "awf_cancel_workspace",
            {
                "workspace_id": "ws_control",
                "reason": "stale task",
                "stop_stack": False,
                "idempotency_key": "ik-cancel",
            },
        )

        assert service.calls == [
            (
                "cancel",
                {
                    "workspace_id": "ws_control",
                    "reason": "stale task",
                    "stop_stack": False,
                    "idempotency_key": "ik-cancel",
                    "expected_version": None,
                },
            )
        ]
        assert payload == {
            "workspace_id": "ws_control",
            "operation_id": "op_cancel",
            "operation_status": "succeeded",
            "status": "cancelled",
            "message": "workspace cancellation requested",
            "warnings": [],
        }

    @pytest.mark.unit
    async def test_stop_workspace_calls_service_and_returns_structured_response(
        self,
    ) -> None:
        service = _RecordingControlService()
        mcp = build_mcp_server(service=service)  # type: ignore[arg-type]

        payload = await _call(
            mcp,
            "awf_stop_workspace",
            {
                "workspace_id": "ws_control",
                "reason": "free local resources",
                "idempotency_key": "ik-stop",
            },
        )

        assert service.calls == [
            (
                "stop",
                {
                    "workspace_id": "ws_control",
                    "reason": "free local resources",
                    "idempotency_key": "ik-stop",
                    "expected_version": None,
                },
            )
        ]
        assert payload == {
            "workspace_id": "ws_control",
            "operation_id": "op_stop",
            "operation_status": "succeeded",
            "status": "cancelled",
            "message": "workspace stack stopped",
            "warnings": [],
        }

    @pytest.mark.unit
    async def test_destroy_workspace_calls_service_and_returns_structured_response(
        self,
    ) -> None:
        service = _RecordingControlService()
        mcp = build_mcp_server(service=service)  # type: ignore[arg-type]

        payload = await _call(
            mcp,
            "awf_destroy_workspace",
            {
                "workspace_id": "ws_control",
                "force": True,
                "remove_volumes": False,
                "remove_worktree": False,
                "idempotency_key": "ik-destroy",
            },
        )

        assert service.calls == [
            (
                "destroy",
                {
                    "workspace_id": "ws_control",
                    "force": True,
                    "remove_volumes": False,
                    "remove_worktree": False,
                    "idempotency_key": "ik-destroy",
                    "expected_version": None,
                },
            )
        ]
        assert payload == {
            "workspace_id": "ws_control",
            "operation_id": "op_destroy",
            "operation_status": "succeeded",
            "status": "destroyed",
            "message": "workspace destroyed",
            "warnings": [],
        }

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("tool_name", "expected_message"),
        [
            ("awf_cancel_workspace", "cancel refused"),
            ("awf_stop_workspace", "stop refused"),
        ],
    )
    async def test_control_tool_errors_return_structured_mcp_error(
        self,
        tool_name: str,
        expected_message: str,
    ) -> None:
        service = _FailingControlService()
        mcp = build_mcp_server(service=service)  # type: ignore[arg-type]

        result = await mcp.call_tool(
            tool_name,
            {"workspace_id": "ws_control", "idempotency_key": "ik-error"},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "NOPE",
            "message": expected_message,
            "detail": None,
        }

    @pytest.mark.unit
    async def test_cancel_workspace_records_operation_through_real_service(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        workspace_id = _workspace_id(created)

        payload = await _call(
            mcp,
            "awf_cancel_workspace",
            {
                "workspace_id": workspace_id,
                "reason": "no longer needed",
                "stop_stack": False,
                "idempotency_key": "ik-real-cancel",
            },
        )
        operations_payload = await _call(
            mcp,
            "awf_list_workspace_operations",
            {"workspace_id": workspace_id},
        )

        assert payload["workspace_id"] == workspace_id  # type: ignore[index]
        assert payload["status"] == "cancelled"  # type: ignore[index]
        assert payload["message"] == "workspace cancellation requested"  # type: ignore[index]
        assert isinstance(operations_payload, dict)
        operations = operations_payload["items"]
        assert isinstance(operations, list)
        assert operations_payload["has_more"] is False
        assert operations[0]["type"] == "cancel"
        assert operations[0]["status"] == "succeeded"
        assert operations[0]["payload"] == {
            "owner": "operator_api",
            "source": "operator_api",
            "reason": "no longer needed",
            "reason_code": "OPERATOR_CANCEL",
            "requested_action": "cancel",
            "stop_stack": False,
        }
        assert operations[0]["idempotency_key"] == "ik-real-cancel"
        assert operations[0]["result"] == {"status": "cancelled"}

    @pytest.mark.unit
    async def test_destroy_workspace_requires_force_for_active_workspace(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        from mcp.types import CallToolResult

        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        workspace_id = _workspace_id(created)

        result = await mcp.call_tool(
            "awf_destroy_workspace",
            {"workspace_id": workspace_id, "idempotency_key": "ik-destroy-active"},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "WORKSPACE_ACTIVE",
            "message": "Active workspaces require force=true before destroy.",
            "detail": None,
        }
