"""MCP server + tool behaviour tests.

We exercise the tools via ``mcp.call_tool(name, args)`` (FastMCP's in-process
harness) against a throwaway PostgreSQL. This validates:
- All tools are registered under the expected names.
- Each tool's happy path returns the same payload shape as the REST API.
- wait_for_workspace exits on terminal state without hanging.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import OperationResponse, WorkspaceControlResponse
from awf.common.config import Settings
from awf.common.github_client import PullRequestAdoptionMetadata, RepoRef
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.mcp import server as mcp_server
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.runtime.logs import LogStore
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


_CREATE_ARGS: dict[str, object] = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "branch_base": "development",
    "task_title": "Add docstring",
    "task_prompt": "Add a one-line docstring to src/module/__init__.py.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
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
        item for item in any_of if isinstance(item, dict) and item.get("type") == "string"
    )
    assert isinstance(string_schema, dict)
    return string_schema


def _assert_idempotency_key_schema(schema: dict[str, object]) -> None:
    string_schema = _optional_string_schema(schema)
    assert str(schema["description"]).startswith("Required idempotency key")
    assert schema["minLength"] == 1
    assert string_schema["maxLength"] == 128
    assert "default" not in schema


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
            "awf_create_workspace_v2",
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
        } <= names

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
    async def test_create_workspace_v2_owned_paths_declares_item_constraints(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = await mcp.list_tools()
        create_v2 = next(tool for tool in tools if tool.name == "awf_create_workspace_v2")
        owned_paths = create_v2.inputSchema["properties"]["owned_paths"]

        assert owned_paths["maxItems"] == 128
        assert owned_paths["items"] == {
            "maxLength": 512,
            "minLength": 1,
            "type": "string",
        }
        assert (
            create_v2.inputSchema["properties"]["provider_readiness_override"]["default"] is False
        )
        assert (
            create_v2.inputSchema["properties"]["provider_readiness_override_reason"]["default"]
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
            },
        )

        assert isinstance(payload, dict)
        assert payload["workspace_id"].startswith("ws_")
        assert payload["repo_slug"] == "dimileeh/aira-web"
        assert payload["pr_number"] == 277
        assert payload["head_ref"] == "feature/ready"
        assert payload["auto_merge"] is False

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


class TestCreateWorkspace:
    @pytest.mark.unit
    async def test_happy_path_returns_accepted_payload(self, mcp) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)

        assert isinstance(payload, dict)
        workspace_id = str(payload["workspace_id"])
        assert payload["status"] == "requested"
        assert workspace_id.startswith("ws_")
        assert payload["status_url"] == f"/v1/workspaces/{workspace_id}"
        assert payload["events_url"] == f"/v1/workspaces/{workspace_id}/events"
        assert "accepted_at" in payload
        assert "id" not in payload
        assert "task_title" not in payload

    @pytest.mark.unit
    async def test_idempotency_key_replays_or_conflicts(self, mcp) -> None:  # type: ignore[no-untyped-def]
        args = {**_CREATE_ARGS, "idempotency_key": "mcp-create-v1-replay"}

        first = await _call(mcp, "awf_create_workspace", args)
        replay = await _call(mcp, "awf_create_workspace", args)
        conflict = await mcp.call_tool(
            "awf_create_workspace",
            {**args, "task_title": "Changed MCP idempotency title"},
        )

        assert isinstance(first, dict)
        assert isinstance(replay, dict)
        assert _workspace_id(replay) == _workspace_id(first)
        assert isinstance(conflict, CallToolResult)
        assert conflict.isError is True
        assert conflict.structuredContent is not None
        assert conflict.structuredContent["error_code"] == "IDEMPOTENCY_CONFLICT"

    @pytest.mark.parametrize(
        ("changed_field", "changed_value"),
        [
            ("env_profile", "profile-b"),
            ("task_external_id", "TASK-B"),
        ],
    )
    @pytest.mark.unit
    async def test_v1_idempotency_conflicts_on_profile_and_external_id(
        self,
        mcp,
        changed_field: str,
        changed_value: str,
    ) -> None:  # type: ignore[no-untyped-def]
        args = {
            **_CREATE_ARGS,
            "env_profile": "profile-a",
            "task_external_id": "TASK-A",
            "idempotency_key": "mcp-create-v1-user-fields",
        }

        first = await _call(mcp, "awf_create_workspace", args)
        conflict = await mcp.call_tool(
            "awf_create_workspace",
            {**args, changed_field: changed_value},
        )

        assert isinstance(first, dict)
        assert isinstance(conflict, CallToolResult)
        assert conflict.isError is True
        assert conflict.structuredContent is not None
        assert conflict.structuredContent["error_code"] == "IDEMPOTENCY_CONFLICT"

    @pytest.mark.unit
    async def test_rejects_unknown_agent(self, mcp) -> None:  # type: ignore[no-untyped-def]
        bad = {**_CREATE_ARGS, "agent": "not-a-real-cli"}
        from mcp.shared.exceptions import McpError  # imported lazily to keep top clean

        with pytest.raises((McpError, Exception)):
            await _call(mcp, "awf_create_workspace", bad)


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


class TestCreateWorkspaceV2:
    @pytest.fixture(autouse=True)
    def _clear_provider_auth_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in _PROVIDER_AUTH_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

    @pytest.mark.unit
    async def test_persists_clean_v2_contract_fields(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        payload = await _call(
            mcp,
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/app.git",
                "base_branch": "main",
                "task_title": "Add planner hook",
                "task_prompt": "Implement the planner hook.",
                "task_kind": "refactor_task",
                "agent": "claude_code",
                "model": "claude-opus-4-7",
                "task_external_id": "AIRA-42",
                "profile_ref": "python",
                "profile": {
                    "name": "inline-python",
                    "validation": {"requested_tier": 2},
                    "monitor": {"initial_review_grace_period_seconds": 333},
                },
                "validation_commands": ["uv run pytest tests/unit -q"],
                "requested_tier": 2,
                "auto_merge": False,
                "initial_review_grace_period_seconds": 12.5,
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp test override",
            },
        )

        assert isinstance(payload, dict)
        ws_id = payload["workspace_id"]
        assert payload["status_url"] == f"/v1/workspaces/{ws_id}"
        assert payload["events_url"] == f"/v1/workspaces/{ws_id}/events"
        assert "accepted_at" in payload
        assert "id" not in payload
        assert "task_class" not in payload
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(str(ws_id))

        assert ws is not None
        assert ws.repo_url == "git@github.com:example/app.git"
        assert ws.branch_base == "main"
        assert ws.task_title == "Add planner hook"
        assert ws.task_prompt == "Implement the planner hook."
        assert ws.task_external_id == "AIRA-42"
        assert ws.task_kind == "refactor_task"
        assert ws.agent == "claude_code"
        assert ws.task_policy["agent_model"] == "claude-opus-4-7"
        assert ws.task_policy["provider_readiness_preflight"]["provider"] == "claude_code"
        assert ws.profile_ref == "python"
        assert ws.requested_profile is not None
        assert ws.requested_profile["name"] == "inline-python"
        assert ws.resolved_profile is not None
        assert ws.resolved_profile["validation"]["requested_tier"] == 2
        assert [item["command"] for item in ws.resolved_profile["phases"]["validate"]] == [
            "uv run pytest tests/unit -q"
        ]
        assert ws.test_commands == ["uv run pytest tests/unit -q"]
        assert ws.auto_merge is False
        assert ws.initial_review_grace_period_seconds == 12.5

    @pytest.mark.unit
    async def test_policy_metadata_round_trips_through_create_get_and_list(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        created = await _call(
            mcp,
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/docs.git",
                "base_branch": "main",
                "task_title": "Document policy metadata",
                "task_prompt": "Update the docs.",
                "task_kind": "feature_branch_pr",
                "task_class": "docs_task",
                "owned_paths": ["README.md", "docs/**"],
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp metadata test fixture",
            },
        )

        assert isinstance(created, dict)
        ws_id = created["workspace_id"]
        fetched = await _call(mcp, "awf_get_workspace", {"workspace_id": ws_id})
        listed = await _call(mcp, "awf_list_workspaces", {"limit": 10})

        assert fetched is not None
        assert fetched["task_class"] == "docs_task"  # type: ignore[index]
        assert fetched["owned_paths"] == ["README.md", "docs/**"]  # type: ignore[index]
        assert isinstance(listed, list)
        assert listed[0]["task_class"] == "docs_task"
        assert listed[0]["owned_paths"] == ["README.md", "docs/**"]

    @pytest.mark.unit
    async def test_create_workspace_v2_returns_structured_provider_preflight_error(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(
            factory,
            settings=Settings(
                _env_file=None,
                host_home=str(tmp_path / "home"),
                docker_host="",
            ),
        )
        mcp = build_mcp_server(service=service)

        result = await mcp.call_tool(
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/docs.git",
                "base_branch": "main",
                "task_title": "Document provider preflight",
                "task_prompt": "Update the docs.",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "PROVIDER_READINESS_PRECHECK_FAILED"
        preflight = result.structuredContent["detail"]["provider_readiness_preflight"]
        assert preflight["provider"] == "codex"
        assert preflight["model"] == "gpt-5.5"
        assert preflight["blocks_launch"] is True

    @pytest.mark.unit
    async def test_create_workspace_v2_rejects_insufficient_disk_without_creating_row(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(
            _env_file=None,
            work_dir=str(tmp_path / "awf-state"),
        )
        mcp = build_mcp_server(
            service=WorkspaceService(factory, settings=settings),
            settings=settings,
            disk_check_provider=_low_disk_check,
        )

        result = await mcp.call_tool(
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/docs.git",
                "base_branch": "main",
                "task_title": "Document low disk admission",
                "task_prompt": "Update the docs.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp disk admission test fixture",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INSUFFICIENT_DISK"
        assert result.structuredContent["detail"]["disk"]["reason"] == "INSUFFICIENT_DISK"
        async with factory() as session:
            rows = await WorkspaceRepository(session).list(limit=10)
        assert rows == []

    @pytest.mark.unit
    async def test_create_workspace_v2_idempotency_key_still_checks_disk_for_new_workspace(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(
            _env_file=None,
            work_dir=str(tmp_path / "awf-state"),
        )
        mcp = build_mcp_server(
            service=WorkspaceService(factory, settings=settings),
            settings=settings,
            disk_check_provider=_low_disk_check,
        )

        result = await mcp.call_tool(
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/docs.git",
                "base_branch": "main",
                "task_title": "Document low disk idempotent admission",
                "task_prompt": "Update the docs.",
                "idempotency_key": "mcp-create-v2-low-disk",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "mcp disk admission test fixture",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INSUFFICIENT_DISK"
        assert result.structuredContent["detail"]["disk"]["reason"] == "INSUFFICIENT_DISK"
        async with factory() as session:
            rows = await WorkspaceRepository(session).list(limit=10)
        assert rows == []

    @pytest.mark.unit
    async def test_create_workspace_v2_override_returns_preflight_summary(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(
            factory,
            settings=Settings(
                _env_file=None,
                host_home=str(tmp_path / "home"),
                docker_host="",
            ),
        )
        mcp = build_mcp_server(service=service)

        payload = await _call(
            mcp,
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/docs.git",
                "base_branch": "main",
                "task_title": "Document provider preflight override",
                "task_prompt": "Update the docs.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "operator verified local auth",
            },
        )

        assert isinstance(payload, dict)
        preflight = payload["provider_readiness_preflight"]
        assert preflight["provider"] == "codex"
        assert preflight["override_used"] is True
        assert preflight["override_reason"] == "operator verified local auth"

    @pytest.mark.unit
    async def test_create_workspace_v2_idempotency_key_replays_or_conflicts(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        args = {
            "repo_url": "git@github.com:example/docs.git",
            "base_branch": "main",
            "task_title": "Document MCP idempotency",
            "task_prompt": "Update the docs.",
            "idempotency_key": "mcp-create-v2-replay",
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "mcp idempotency test fixture",
        }

        first = await _call(mcp, "awf_create_workspace_v2", args)
        replay = await _call(mcp, "awf_create_workspace_v2", args)
        conflict = await mcp.call_tool(
            "awf_create_workspace_v2",
            {**args, "task_title": "Changed MCP idempotency title"},
        )

        assert isinstance(first, dict)
        assert isinstance(replay, dict)
        assert replay["workspace_id"] == first["workspace_id"]
        assert isinstance(conflict, CallToolResult)
        assert conflict.isError is True
        assert conflict.structuredContent is not None
        assert conflict.structuredContent["error_code"] == "IDEMPOTENCY_CONFLICT"

    @pytest.mark.unit
    async def test_create_workspace_v2_idempotency_replay_skips_disk_check(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(
            _env_file=None,
            work_dir=str(tmp_path / "awf-state"),
        )
        calls = 0

        def counted_disk_check(settings: Settings) -> DiskCheck:
            nonlocal calls
            calls += 1
            return _ok_disk_check(settings)

        mcp = build_mcp_server(
            service=WorkspaceService(factory, settings=settings),
            settings=settings,
            disk_check_provider=counted_disk_check,
        )
        args = {
            "repo_url": "git@github.com:example/docs.git",
            "base_branch": "main",
            "task_title": "Document MCP idempotency disk replay",
            "task_prompt": "Update the docs.",
            "idempotency_key": "mcp-create-v2-replay-disk",
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "mcp idempotency disk test fixture",
        }

        first = await _call(mcp, "awf_create_workspace_v2", args)
        replay = await _call(mcp, "awf_create_workspace_v2", args)

        assert isinstance(first, dict)
        assert isinstance(replay, dict)
        assert replay["workspace_id"] == first["workspace_id"]
        assert calls == 1

    @pytest.mark.unit
    async def test_create_workspace_v2_external_id_scope_conflict_returns_structured_error(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        external_id = "mcp-create-v2-external-id-conflict"
        args = {
            "repo_url": "git@github.com:example/docs.git",
            "base_branch": "main",
            "task_title": "Document external id",
            "task_prompt": "Update the docs.",
            "task_external_id": external_id,
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "mcp external id conflict test fixture",
        }

        created = await _call(mcp, "awf_create_workspace_v2", args)
        conflict = await mcp.call_tool(
            "awf_create_workspace_v2",
            {**args, "base_branch": "release/next"},
        )

        assert isinstance(created, dict)
        assert isinstance(conflict, CallToolResult)
        assert conflict.isError is True
        assert conflict.structuredContent == {
            "error_code": "TASK_EXTERNAL_ID_CONFLICT",
            "message": (
                "Task external_id is already associated with a different "
                "repo/base/task-class/owned-path scope; use a unique "
                "external_id for this backlog slice or retry the original scope."
            ),
            "detail": {"external_id": external_id},
        }

    @pytest.mark.unit
    async def test_retry_workspace_provider_preflight_error_and_override(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(
            factory,
            settings=Settings(
                _env_file=None,
                host_home=str(tmp_path / "home"),
                docker_host="",
            ),
        )
        mcp = build_mcp_server(service=service)
        created = await _call(
            mcp,
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/retry.git",
                "base_branch": "main",
                "task_title": "Retry with provider preflight",
                "task_prompt": "Update the docs.",
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "initial override",
            },
        )
        assert isinstance(created, dict)
        workspace_id = str(created["workspace_id"])
        async with factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            assert workspace is not None
            await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
            await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
            await session.commit()

        blocked = await mcp.call_tool(
            "awf_retry_workspace",
            {"workspace_id": workspace_id},
        )
        assert isinstance(blocked, CallToolResult)
        assert blocked.isError is True
        assert blocked.structuredContent is not None
        assert blocked.structuredContent["error_code"] == "PROVIDER_READINESS_PRECHECK_FAILED"
        blocked_preflight = blocked.structuredContent["detail"]["provider_readiness_preflight"]
        assert blocked_preflight["provider"] == "codex"
        assert blocked_preflight["source_workspace_id"] == workspace_id

        retried = await _call(
            mcp,
            "awf_retry_workspace",
            {
                "workspace_id": workspace_id,
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "retry override",
            },
        )

        assert isinstance(retried, dict)
        preflight = retried["provider_readiness_preflight"]
        assert preflight["source_workspace_id"] == workspace_id
        assert preflight["override_used"] is True
        assert preflight["override_reason"] == "retry override"

    @pytest.mark.unit
    async def test_retry_workspace_returns_structured_retry_error_for_missing_workspace(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_retry_workspace",
            {"workspace_id": "ws_missing_retry"},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "WORKSPACE_NOT_FOUND"

    @pytest.mark.unit
    async def test_observability_list_tools_return_invalid_cursor_errors(
        self,
        factory: async_sessionmaker[AsyncSession],
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/observability.git",
                branch_base="main",
                task_title="Observe cursor handling",
                task_prompt="Exercise invalid cursors.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        for tool_name in (
            "awf_list_workspace_validation",
            "awf_list_workspace_stale_reasons",
            "awf_list_workspace_artifacts",
        ):
            result = await mcp.call_tool(
                tool_name,
                {"workspace_id": workspace.id, "cursor": "not-valid-cursor"},
            )
            assert isinstance(result, CallToolResult)
            assert result.isError is True
            assert result.structuredContent is not None
            assert result.structuredContent["error_code"] == "INVALID_CURSOR"

    @pytest.mark.unit
    async def test_core_readiness_rejects_unknown_strict_provider(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_get_core_release_readiness",
            {"providers": ["bogus-provider"]},
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INVALID_PROVIDERS"

    @pytest.mark.unit
    async def test_unknown_profile_ref_returns_structured_invalid_profile_error(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        from mcp.types import CallToolResult

        result = await mcp.call_tool(
            "awf_create_workspace_v2",
            {
                "repo_url": "git@github.com:example/app.git",
                "base_branch": "main",
                "task_title": "Add planner hook",
                "task_prompt": "Implement the planner hook.",
                "profile_ref": "missing-profile",
            },
        )

        message = "unknown workspace profile_ref: missing-profile"
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_PROFILE",
            "message": message,
            "detail": None,
        }
        assert result.content[0].type == "text"


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
                "agent": "gemini",
            },
        )
        wrong_status = await _call(
            mcp,
            "awf_create_workspace",
            {
                **_CREATE_ARGS,
                "repo_url": repo_url,
                "task_title": "wrong status",
                "agent": "gemini",
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
                "agent": "gemini",
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
                "agent": "gemini",
                "repo_url": repo_url,
                "limit": 10,
            },
        )

        assert isinstance(listed, list)
        assert [row["id"] for row in listed] == [_workspace_id(matching)]
        assert _workspace_id(wrong_status) not in [row["id"] for row in listed]


class TestWaitForWorkspace:
    @pytest.mark.unit
    async def test_exits_immediately_when_already_terminal(self, mcp) -> None:  # type: ignore[no-untyped-def]
        # Simulate a workspace that's already terminal by creating one and
        # configuring the terminal_statuses to include 'requested'.
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)

        result = await _call(
            mcp,
            "awf_wait_for_workspace",
            {
                "workspace_id": ws_id,
                "terminal_statuses": ["requested"],
                "poll_interval_seconds": 0.1,
                "timeout_seconds": 5.0,
            },
        )
        assert result is not None
        assert result["status"] == "requested"  # type: ignore[index]

    @pytest.mark.unit
    async def test_returns_current_state_on_timeout(self, mcp) -> None:  # type: ignore[no-untyped-def]
        created = await _call(mcp, "awf_create_workspace", _CREATE_ARGS)
        ws_id = _workspace_id(created)

        # Pick terminal statuses the workspace will never reach + tight timeout.
        result = await _call(
            mcp,
            "awf_wait_for_workspace",
            {
                "workspace_id": ws_id,
                "terminal_statuses": ["completed", "failed"],
                "poll_interval_seconds": 0.1,
                "timeout_seconds": 1.0,
            },
        )
        # On timeout we still return the current state (status=requested).
        assert result is not None
        assert result["status"] == "requested"  # type: ignore[index]

    @pytest.mark.unit
    async def test_returns_none_for_unknown_id(self, mcp) -> None:  # type: ignore[no-untyped-def]
        result = await _call(
            mcp,
            "awf_wait_for_workspace",
            {
                "workspace_id": "ws_never_existed",
                "poll_interval_seconds": 0.1,
                "timeout_seconds": 1.0,
            },
        )
        assert result is None


class TestWorkspaceEvents:
    @pytest.mark.unit
    async def test_lists_requested_workspace_events_with_limit_and_type(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        first = await _call(mcp, "awf_create_workspace", {**_CREATE_ARGS, "task_title": "first"})
        second = await _call(
            mcp,
            "awf_create_workspace",
            {**_CREATE_ARGS, "task_title": "second"},
        )
        first_id = _workspace_id(first)
        second_id = _workspace_id(second)
        base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)

        async with factory() as session:
            repo = WorkspaceRepository(session)
            first_ws = await repo.get(first_id)
            second_ws = await repo.get(second_id)
            assert first_ws is not None
            assert second_ws is not None
            first_old = await repo.add_event(
                first_ws,
                event_type="workspace.phase_started",
                reason_code="OLD",
                payload={"phase": "agent"},
            )
            first_new = await repo.add_event(
                first_ws,
                event_type="workspace.phase_started",
                reason_code="NEW",
                payload={"phase": "validation"},
            )
            wrong_workspace = await repo.add_event(
                second_ws,
                event_type="workspace.phase_started",
                reason_code="OTHER",
                payload={"phase": "validation"},
            )
            ignored_type = await repo.add_event(
                first_ws,
                event_type="workspace.log",
                reason_code="IGNORED",
                payload={"stream": "agent.stdout"},
            )
            first_old.occurred_at = base
            first_new.occurred_at = base + timedelta(seconds=2)
            wrong_workspace.occurred_at = base + timedelta(seconds=3)
            ignored_type.occurred_at = base + timedelta(seconds=4)
            await session.commit()

        events = await _call(
            mcp,
            "awf_list_workspace_events",
            {
                "workspace_id": first_id,
                "event_type": "workspace.phase_started",
                "limit": 1,
            },
        )

        assert isinstance(events, list)
        assert [event["workspace_id"] for event in events] == [first_id]
        assert [event["reason_code"] for event in events] == ["NEW"]
        assert [event["payload"] for event in events] == [{"phase": "validation"}]

    @pytest.mark.unit
    async def test_missing_workspace_events_return_none(self, mcp) -> None:  # type: ignore[no-untyped-def]
        result = await _call(
            mcp,
            "awf_list_workspace_events",
            {"workspace_id": "ws_missing"},
        )

        assert result is None


class TestWorkspaceRuntime:
    @pytest.mark.unit
    async def test_get_workspace_runtime_returns_container_snapshot(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        class FakeRuntimeInspector:
            async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
                assert compose_project_name == "awf_ws_mcp_runtime"
                return RuntimeSnapshot(
                    stack_state="running",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="abc123",
                            image="awf-agent-runtime:latest",
                            state="running",
                            status="Up 1 minute",
                            health="healthy",
                            ports=["127.0.0.1:8000->8000/tcp"],
                            started_at="2026-04-25T10:00:00Z",
                        )
                    ],
                )

        service = WorkspaceService(factory, runtime_inspector=FakeRuntimeInspector())
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe runtime",
                task_prompt="Inspect runtime.",
                agent="codex",
                test_commands=[],
            )
            workspace.compose_project_name = "awf_ws_mcp_runtime"
            await session.commit()

        runtime = await _call(
            mcp,
            "awf_get_workspace_runtime",
            {"workspace_id": workspace.id},
        )

        assert runtime == {
            "workspace_id": workspace.id,
            "compose_project_name": "awf_ws_mcp_runtime",
            "stack_state": "running",
            "services": [
                {
                    "name": "agent",
                    "container_id": "abc123",
                    "image": "awf-agent-runtime:latest",
                    "state": "running",
                    "status": "Up 1 minute",
                    "health": "healthy",
                    "ports": ["127.0.0.1:8000->8000/tcp"],
                    "started_at": "2026-04-25T10:00:00Z",
                }
            ],
            "app_endpoints": [],
            "logs_available": True,
            "control_available": True,
            "reason": None,
        }

    @pytest.mark.unit
    async def test_get_workspace_runtime_missing_workspace_returns_none(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await _call(
            mcp,
            "awf_get_workspace_runtime",
            {"workspace_id": "ws_missing"},
        )

        assert result is None


class TestWorkspaceOperations:
    @pytest.mark.unit
    async def test_list_workspace_operations_respects_limit(
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

        payload = await _call(
            mcp,
            "awf_list_workspace_operations",
            {"workspace_id": workspace.id, "limit": 2},
        )

        assert isinstance(payload, dict)
        assert [item["id"] for item in payload["items"]] == [stop.id, validate.id]
        assert [item["type"] for item in payload["items"]] == ["stop", "validate"]
        assert [item["status"] for item in payload["items"]] == ["pending", "running"]
        assert payload["has_more"] is True
        assert payload["next_cursor"] is not None
        assert payload["limit"] == 2
        assert payload["cursor"] is None

        second_page = await _call(
            mcp,
            "awf_list_workspace_operations",
            {
                "workspace_id": workspace.id,
                "limit": 2,
                "cursor": payload["next_cursor"],
            },
        )

        assert isinstance(second_page, dict)
        assert [item["id"] for item in second_page["items"]] == [create.id]
        assert second_page["has_more"] is False
        assert second_page["next_cursor"] is None
        assert second_page["cursor"] == payload["next_cursor"]

    @pytest.mark.unit
    async def test_list_workspace_operations_forwards_status_and_type_filters(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Filter workspace operations",
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
            running_validate = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.validate,
                status=OperationStatus.running,
            )
            running_create = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.create,
                status=OperationStatus.running,
            )
            pending_validate = await repo.create(
                workspace_id=workspace.id,
                operation_type=OperationType.validate,
                status=OperationStatus.pending,
            )
            create.created_at = base
            running_validate.created_at = base + timedelta(seconds=1)
            running_create.created_at = base + timedelta(seconds=2)
            pending_validate.created_at = base + timedelta(seconds=3)
            await session.commit()

        payload = await _call(
            mcp,
            "awf_list_workspace_operations",
            {
                "workspace_id": workspace.id,
                "status": "running",
                "operation_type": "validate",
            },
        )

        assert isinstance(payload, dict)
        assert [item["id"] for item in payload["items"]] == [running_validate.id]
        assert [item["type"] for item in payload["items"]] == ["validate"]
        assert [item["status"] for item in payload["items"]] == ["running"]
        assert payload["has_more"] is False
        assert payload["limit"] == 50

    @pytest.mark.unit
    async def test_list_workspace_operations_missing_workspace_returns_not_found_error(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_list_workspace_operations", {"workspace_id": "ws_missing"}
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "NOT_FOUND",
            "message": "No workspace with id ws_missing",
            "detail": None,
        }

    @pytest.mark.unit
    async def test_list_workspace_operations_rejects_invalid_cursor(
        self,
        mcp,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:  # type: ignore[no-untyped-def]
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Reject bad operation cursor",
                task_prompt="Exercise invalid operation cursor.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        result = await mcp.call_tool(
            "awf_list_workspace_operations",
            {
                "workspace_id": workspace.id,
                "limit": 2,
                "cursor": "not-valid-cursor",
            },
        )

        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_CURSOR",
            "message": "Invalid operation list cursor.",
            "detail": None,
        }


class TestWorkspaceLogs:
    @pytest.mark.unit
    async def test_lists_and_reads_indexed_log_streams(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        store = LogStore(root=tmp_path / "logs", session_factory=factory)
        sink = await store.open_stream(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
            source="agent",
            name="Agent stdout",
            kind="stdout",
        )
        await sink.write("alpha\nbeta\n")
        await sink.close()

        listed = await _call(
            mcp,
            "awf_list_workspace_logs",
            {"workspace_id": workspace.id},
        )
        assert isinstance(listed, dict)
        assert [stream["stream_id"] for stream in listed["items"]] == ["agent.stdout"]
        assert listed["items"][0]["byte_count"] == len("alpha\nbeta\n")
        assert listed["items"][0]["line_count"] == 2
        assert listed["limit"] == 1

        chunk = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": 6,
                "limit_bytes": 4,
            },
        )
        assert chunk == {
            "stream_id": "agent.stdout",
            "offset": 6,
            "next_offset": 10,
            "eof": False,
            "data": "beta",
        }

        eof = await _call(
            mcp,
            "awf_read_workspace_log",
            {
                "workspace_id": workspace.id,
                "stream_id": "agent.stdout",
                "offset": len("alpha\nbeta\n"),
                "limit_bytes": 16,
            },
        )
        assert eof == {
            "stream_id": "agent.stdout",
            "offset": len("alpha\nbeta\n"),
            "next_offset": len("alpha\nbeta\n"),
            "eof": True,
            "data": "",
        }

    @pytest.mark.unit
    async def test_missing_workspace_or_stream_returns_none(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        service = WorkspaceService(factory, log_root=tmp_path / "logs")
        mcp = build_mcp_server(service=service)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Observe logs",
                task_prompt="Write logs.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()

        missing_workspace = await _call(
            mcp,
            "awf_list_workspace_logs",
            {"workspace_id": "ws_missing"},
        )
        missing_stream = await _call(
            mcp,
            "awf_read_workspace_log",
            {"workspace_id": workspace.id, "stream_id": "agent.stderr"},
        )

        assert missing_workspace is None
        assert missing_stream is None


class TestReadWorkspaceArtifact:
    @pytest.mark.unit
    async def test_tool_registered_and_bounded(self, mcp) -> None:  # type: ignore[no-untyped-def]
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        assert "awf_read_workspace_artifact" in tools
        schema = tools["awf_read_workspace_artifact"].inputSchema
        props = schema["properties"]
        assert "workspace_id" in schema.get("required", [])
        assert "relative_path" in schema.get("required", [])
        assert "limit_bytes" not in schema.get("required", [])
        assert props["limit_bytes"]["default"] == 65_536
        assert props["limit_bytes"]["minimum"] == 1
        assert "maximum" not in props["limit_bytes"]

    @pytest.mark.unit
    async def test_reads_safe_small_file_and_returns_base64_content(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact read",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"hello artifact\n"
        (artifact_dir / "report.txt").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "report.txt"},
        )

        assert isinstance(result, dict)
        assert result["workspace_id"] == workspace.id
        assert result["relative_path"] == "report.txt"
        assert result["name"] == "report.txt"
        assert result["content_type"] == "text/plain"
        assert result["size_bytes"] == len(payload)
        assert base64.b64decode(result["content"]) == payload

    @pytest.mark.unit
    async def test_missing_workspace_returns_not_found(
        self,
        mcp,
    ) -> None:  # type: ignore[no-untyped-def]
        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": "ws_missing", "relative_path": "report.txt"},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "NOT_FOUND"

    @pytest.mark.unit
    async def test_missing_file_returns_not_found(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact read missing",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "missing.txt"},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "NOT_FOUND"
        assert "missing.txt" in result.structuredContent["message"]
        # must not leak absolute host path
        assert str(artifact_dir) not in str(result.structuredContent.get("detail", ""))

    @pytest.mark.unit
    async def test_symlink_escape_returns_not_found(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact symlink",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        (artifact_dir / "link.txt").symlink_to(outside)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "link.txt"},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "NOT_FOUND"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "bad_path",
        [
            "../secret.txt",
            "/tmp/secret.txt",
            "",
            "reports\\summary.json",
        ],
    )
    async def test_invalid_paths_return_invalid_artifact_path(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        bad_path: str,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact bad path",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": bad_path},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INVALID_ARTIFACT_PATH"

    @pytest.mark.unit
    async def test_oversized_file_returns_artifact_oversized(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact oversized",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "big.bin").write_bytes(b"x" * 200)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "big.bin", "limit_bytes": 100},
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "ARTIFACT_OVERSIZED"
        assert result.structuredContent.get("detail") is not None
        assert isinstance(result.structuredContent["detail"], dict)
        assert result.structuredContent["detail"]["limit_bytes"] == 100
        assert result.structuredContent["detail"]["actual_bytes"] == 200

    @pytest.mark.unit
    async def test_rejects_limit_bytes_above_ceiling(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact ceiling",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "small.bin").write_bytes(b"x")

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "small.bin",
                "limit_bytes": 2_000_000,
            },
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "ARTIFACT_OVERSIZED"
        assert result.structuredContent.get("detail") is not None
        assert isinstance(result.structuredContent["detail"], dict)
        assert result.structuredContent["detail"]["limit_bytes"] == 2_000_000
        assert result.structuredContent["detail"]["actual_bytes"] is None

    @pytest.mark.unit
    async def test_respects_explicit_limit_bytes_within_ceiling(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact limit ok",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"x" * 50
        (artifact_dir / "medium.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "medium.bin", "limit_bytes": 100},
        )
        assert isinstance(result, dict)
        assert result["size_bytes"] == len(payload)
        assert base64.b64decode(result["content"]) == payload

    @pytest.mark.unit
    async def test_read_workspace_artifact_redacts_secrets(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact redaction",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = f"prefix {secret} suffix".encode()
        (artifact_dir / "secret.txt").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "secret.txt", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"prefix <redacted> suffix"
        assert result["size_bytes"] == len(payload)

    @pytest.mark.unit
    async def test_utf16le_artifact_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact UTF-16 blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        text = f"prefix {secret} suffix"
        payload = b"\xff\xfe" + text.encode("utf-16le")
        (artifact_dir / "secret_utf16.txt").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "secret_utf16.txt",
                "limit_bytes": 1024,
            },
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_redaction_expansion_triggers_oversized(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "ABCD"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact redaction oversize",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        limit_bytes = 100
        payload = (secret * (limit_bytes // len(secret))).encode()
        (artifact_dir / "secret.txt").write_bytes(payload)

        result = await mcp.call_tool(
            "awf_read_workspace_artifact",
            {
                "workspace_id": workspace.id,
                "relative_path": "secret.txt",
                "limit_bytes": limit_bytes,
            },
        )
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "ARTIFACT_OVERSIZED"
        assert result.structuredContent.get("detail") is not None
        assert isinstance(result.structuredContent["detail"], dict)
        assert result.structuredContent["detail"]["limit_bytes"] == limit_bytes
        assert result.structuredContent["detail"]["actual_bytes"] == (
            (limit_bytes // len(secret)) * len("<redacted>")
        )

    @pytest.mark.unit
    async def test_binary_artifact_containing_secret_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact binary blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"\x00" + f"prefix {secret} suffix".encode() + b"\x00\xff"
        (artifact_dir / "secret.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "secret.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_clean_binary_artifact_is_not_redacted(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact binary no redact",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b"\x00\xff\x01\x02\x03\x04"
        (artifact_dir / "clean.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "clean.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == payload
        assert result["size_bytes"] == len(payload)

    @pytest.mark.unit
    async def test_octet_stream_without_null_bytes_is_redacted(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact octet-stream blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = f"prefix {secret} suffix".encode()
        (artifact_dir / "secret.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "secret.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert secret.encode() not in decoded
        assert decoded == b"prefix <redacted> suffix"

    @pytest.mark.unit
    async def test_octet_stream_with_null_bytes_and_secret_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact octet-stream null pass",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        # Include a null byte + the secret: confirms binary path blocks the artifact
        # rather than silently passing it through as text-redacted content.
        payload = b"\x00" + secret.encode() + b"\x00\xff\x01\x02\x03\x04"
        (artifact_dir / "clean.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "clean.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_binary_artifact_containing_provider_token_pattern_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact binary token pattern blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        # Recognizable provider token not present in settings/env
        payload = b"\x00" + b"ghp_deadbeef1234567890" + b"\x00\xff"
        (artifact_dir / "leaked.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "leaked.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_binary_artifact_containing_url_credentials_is_blocked(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        settings = Settings(_env_file=None, work_dir=str(tmp_path))
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact binary URL credential blocked",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        # URL credential not present in settings/env, wrapped in null bytes to force binary path
        payload = b"\x00" + b"https://user:password@example.com/secret" + b"\x00\xff"
        (artifact_dir / "leaked.bin").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "leaked.bin", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        assert result["error_code"] == "ARTIFACT_BLOCKED"

    @pytest.mark.unit
    async def test_env_artifact_is_redacted(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact env redact",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = f"API_TOKEN={secret}\n".encode()
        (artifact_dir / "config.env").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "config.env", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"API_TOKEN=<redacted>\n"
        assert result["size_bytes"] == len(decoded)

    @pytest.mark.unit
    async def test_yaml_artifact_is_redacted(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact yaml redact",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = f"token: {secret}\n".encode()
        (artifact_dir / "values.yaml").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "values.yaml", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b"token: <redacted>\n"
        assert result["size_bytes"] == len(decoded)

    @pytest.mark.unit
    async def test_json_artifact_is_redacted(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        secret = "test-secret-token-abc"
        settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=secret)
        service = WorkspaceService(factory, settings=settings)
        mcp = build_mcp_server(service=service, settings=settings)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                task_title="Artifact JSON redact",
                task_prompt="Read artifact.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
        artifact_dir = tmp_path / "artifacts" / workspace.id
        artifact_dir.mkdir(parents=True)
        payload = b'{"token": "' + secret.encode() + b'"}'
        (artifact_dir / "config.json").write_bytes(payload)

        result = await _call(
            mcp,
            "awf_read_workspace_artifact",
            {"workspace_id": workspace.id, "relative_path": "config.json", "limit_bytes": 1024},
        )
        assert isinstance(result, dict)
        decoded = base64.b64decode(result["content"])
        assert decoded == b'{"token": "<redacted>"}'
        assert result["size_bytes"] == len(decoded)
        assert result["content_type"] == "application/json"
