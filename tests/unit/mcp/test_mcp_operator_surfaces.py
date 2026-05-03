"""MCP parity tests for read-only operator surfaces."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    StaleReasonCreate,
    StaleReasonRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.mcp import server as mcp_server
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.service.disk import DiskCheck
from awf.service.orphan_resources import (
    WorkspaceIdView,
    build_orphan_resource_summary,
    empty_docker_scan,
    scan_managed_worktrees,
)
from awf.service.readiness import CoreReadinessCheck, CoreReadinessReport
from awf.service.workspace_runtime_health import WorkspaceRuntimeHealthSummary


@dataclass(frozen=True)
class OperatorStack:
    client: AsyncClient
    mcp: Any
    factory: async_sessionmaker[AsyncSession]
    settings: Settings
    auth_headers: dict[str, str]


NEW_OPERATOR_TOOLS = {
    "awf_list_merge_queue",
    "awf_list_workspace_overview",
    "awf_list_workspace_validation",
    "awf_list_workspace_stale_reasons",
    "awf_list_workspace_artifacts",
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
}

BOUNDED_READ_ONLY_LIST_TOOLS = {
    "awf_list_merge_queue",
    "awf_list_workspace_overview",
    "awf_list_workspace_validation",
    "awf_list_workspace_stale_reasons",
    "awf_list_workspace_artifacts",
    "awf_get_failure_analysis_summary",
    "awf_list_operations",
    "awf_get_overlap_graph",
    "awf_list_tasks",
    "awf_list_task_attempts",
    "awf_list_locks",
}

FORBIDDEN_OPERATOR_TOOL_PREFIXES = (
    "awf_shell",
    "awf_exec",
    "awf_run_command",
    "awf_run_shell",
    "awf_docker_exec",
    "awf_container_exec",
    "awf_read_file",
    "awf_list_files",
    "awf_read_secret",
    "awf_list_secret",
    "awf_read_workspace_artifact",
    "awf_download_workspace_artifact",
)

FORBIDDEN_READ_ONLY_INPUTS = {
    "artifact_path",
    "command",
    "container_id",
    "docker_command",
    "host_path",
    "path",
    "secret_name",
    "shell",
}


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> AsyncIterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def operator_stack(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[OperatorStack]:
    work_dir = tmp_path / "awf-state"
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))
    monkeypatch.setenv("AWF_MIN_FREE_DISK_BYTES", "700")
    monkeypatch.setenv("AWF_WORKER_MAX_CONCURRENT_PROVISIONS", "5")
    monkeypatch.setenv("AWF_WORKER_MAX_CONCURRENT_EXECUTIONS", "2")
    get_settings.cache_clear()

    factory = make_session_factory(engine)
    settings = Settings(
        _env_file=None,
        api_token="secret",
        work_dir=str(work_dir),
        min_free_disk_bytes=700,
        worker_max_concurrent_provisions=5,
        worker_max_concurrent_executions=2,
    )
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = _ok_disk_check

    mcp = build_mcp_server(service=WorkspaceService(factory))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield OperatorStack(
            client=client,
            mcp=mcp,
            factory=factory,
            settings=settings,
            auth_headers={"Authorization": "Bearer secret"},
        )


@pytest.fixture
async def resource_stack(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[OperatorStack]:
    work_dir = tmp_path / "awf-state"
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))
    get_settings.cache_clear()

    factory = make_session_factory(engine)
    settings = Settings(
        _env_file=None,
        api_token="secret",
        work_dir=str(work_dir),
        min_free_disk_bytes=700,
        worker_max_concurrent_provisions=5,
        worker_max_concurrent_executions=2,
    )
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = _ok_disk_check
    app.state.orphan_resource_summary_provider = _no_orphan_summary
    app.state.runtime_health_summary_provider = _empty_runtime_health_summary

    mcp = build_mcp_server(
        service=WorkspaceService(factory),
        settings=settings,
        disk_check_provider=_ok_disk_check,
        orphan_resource_summary_provider=_no_orphan_summary,
        runtime_health_summary_provider=_empty_runtime_health_summary,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield OperatorStack(
            client=client,
            mcp=mcp,
            factory=factory,
            settings=settings,
            auth_headers={"Authorization": "Bearer secret"},
        )


def _ok_disk_check(settings: Settings) -> DiskCheck:
    threshold = settings.min_free_disk_bytes
    free = threshold + 1
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=free,
        used_bytes=0,
        free_bytes=free,
        percent_free=100.0,
        threshold_bytes=threshold,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
    )


def _no_orphan_summary(settings: Settings, _session: AsyncSession) -> Any:
    return build_orphan_resource_summary(
        docker_scan=empty_docker_scan(),
        worktree_scan=scan_managed_worktrees(settings.work_dir),
        workspace_view=WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset(),
            available=True,
        ),
    )


def _empty_runtime_health_summary(
    _settings: Settings,
    _session: AsyncSession,
    _orphan_resources: Any,
) -> WorkspaceRuntimeHealthSummary:
    return WorkspaceRuntimeHealthSummary(findings=())


async def _call(mcp: Any, name: str, args: dict[str, object]) -> object:
    result = await mcp.call_tool(name, args)
    if isinstance(result, CallToolResult):
        return result.structuredContent
    _, payload = result
    if isinstance(payload, dict) and list(payload.keys()) == ["result"]:
        return payload["result"]
    return payload


async def _call_result(mcp: Any, name: str, args: dict[str, object]) -> CallToolResult:
    result = await mcp.call_tool(name, args)
    assert isinstance(result, CallToolResult)
    return result


async def _workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    title: str = "Operator parity workspace",
    status: WorkspaceStatus = WorkspaceStatus.requested,
    repo_url: str = "git@github.com:example/operator.git",
    base_branch: str = "main",
    task_class: str | None = "test_task",
    owned_paths: list[str] | None = None,
    updated_at: datetime | None = None,
    failure_reason: FailureReason | str | None = None,
    failure_message: str | None = None,
    task_policy: dict[str, Any] | None = None,
    resolved_profile: dict[str, Any] | None = None,
) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url=repo_url,
            branch_base=base_branch,
            task_title=title,
            task_prompt=f"Implement {title}.",
            task_external_id=f"TASK-{title.replace(' ', '-').upper()}",
            task_class=task_class,
            owned_paths=owned_paths or ["src/awf/**"],
            task_policy=task_policy,
            agent=AgentRuntime.codex.value,
            test_commands=["pytest -q"],
            resolved_profile=resolved_profile,
        )
        workspace.status = status.value
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.pr_url = f"https://github.com/example/operator/pull/{len(title)}"
        workspace.pr_number = len(title)
        workspace.failure_reason = (
            failure_reason.value if isinstance(failure_reason, FailureReason) else failure_reason
        )
        workspace.failure_message = failure_message
        if updated_at is not None:
            workspace.updated_at = updated_at
        await repo.add_event(
            workspace,
            event_type="operator_parity.seed",
            reason_code="TEST_EVENT",
            payload={"title": title},
        )
        await session.commit()
        return workspace.id


async def _seed_merge_queue(factory: async_sessionmaker[AsyncSession]) -> str:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/merge.git",
            branch_base="main",
            task_title="Merge queue parity",
            task_prompt="Expose merge queue MCP parity.",
            task_external_id="MQ-1",
            task_class="test_task",
            owned_paths=["src/awf/api/**"],
            agent=AgentRuntime.codex.value,
            test_commands=["pytest -q"],
        )
        workspace.status = WorkspaceStatus.monitoring_pr.value
        workspace.branch_name = "awf/merge-queue-parity"
        workspace.pr_url = "https://github.com/example/merge/pull/17"
        workspace.pr_number = 17
        workspace.created_at = now
        workspace.updated_at = now
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        attempt.is_canonical_for_merge = True
        candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
            head_sha="h" * 40,
            base_sha="b" * 40,
        )
        candidate.stale = True
        candidate.stale_reason = "STALE_DEPENDENCY"
        await StaleReasonRepository(session).replace_active_findings(
            workspace_id=workspace.id,
            candidate_id=candidate.id,
            attempt_id=attempt.id,
            task_id=task.id,
            findings=[
                StaleReasonCreate(
                    reason_code="STALE_DEPENDENCY",
                    trigger_type="dependency_changed",
                    trigger_ref="uv.lock",
                    explanation="Dependency manifest changed on target branch.",
                )
            ],
        )
        run = await ValidationRunRepository(session).start(
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            tier=1,
            commands=[
                {
                    "phase": "validate",
                    "command_index": 1,
                    "command": "pytest -q",
                    "stream_ids": {
                        "stdout": "validation.01_validate.stdout",
                        "stderr": "validation.01_validate.stderr",
                    },
                }
            ],
            base_commit="base-commit",
            base_sha="b" * 40,
            workspace_head_sha="w" * 40,
            target_branch="main",
            target_head_sha="h" * 40,
            log_stream_refs={"commands": [{"stdout": "validation.01_validate.stdout"}]},
            started_at=now,
        )
        await ValidationRunRepository(session).finish(
            run.id,
            status="succeeded",
            reason_code="VALIDATION_OK",
            finished_at=now + timedelta(minutes=3),
        )
        await repo.add_event(
            workspace,
            event_type="operator_parity.merge_queue",
            reason_code="TEST_EVENT",
            payload={"candidate_id": candidate.id},
        )
        await session.commit()
        return workspace.id


async def _seed_validation(factory: async_sessionmaker[AsyncSession]) -> str:
    now = datetime(2026, 4, 27, 13, 0, tzinfo=UTC)
    workspace_id = await _workspace(
        factory,
        title="Validation parity",
        status=WorkspaceStatus.completed,
        updated_at=now,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        stream_repo = WorkspaceLogStreamRepository(session)
        stdout = await stream_repo.create_or_get(
            workspace_id=workspace_id,
            stream_id="validation.01_validate.stdout",
            source="validation",
            name="validate stdout",
            kind="stdout",
            path="/tmp/validation.stdout.log",
        )
        stderr = await stream_repo.create_or_get(
            workspace_id=workspace_id,
            stream_id="validation.01_validate.stderr",
            source="validation",
            name="validate stderr",
            kind="stderr",
            path="/tmp/validation.stderr.log",
        )
        stdout.opened_at = now
        stderr.opened_at = now
        await stream_repo.append_metadata(
            workspace_id=workspace_id,
            stream_id=stdout.stream_id,
            byte_delta=12,
            line_delta=2,
        )
        await stream_repo.append_metadata(
            workspace_id=workspace_id,
            stream_id=stderr.stream_id,
            byte_delta=5,
            line_delta=1,
        )
        await stream_repo.close(workspace_id=workspace_id, stream_id=stdout.stream_id)
        await stream_repo.close(workspace_id=workspace_id, stream_id=stderr.stream_id)
        run = await ValidationRunRepository(session).start(
            workspace_id=workspace_id,
            attempt_id=None,
            tier=2,
            commands=[
                {
                    "phase": "validate",
                    "command_index": 1,
                    "command": "pytest -q",
                    "stream_ids": {"stdout": stdout.stream_id, "stderr": stderr.stream_id},
                }
            ],
            base_commit="base-persisted",
            base_sha="b" * 40,
            workspace_head_sha="w" * 40,
            target_branch="main",
            target_head_sha="t" * 40,
            profile_name="operator-profile",
            profile_version=3,
            profile_source="inline",
            resolved_profile_digest="p" * 64,
            environment_identity_digest="e" * 64,
            environment_identity_inputs={"python": "3.12"},
            log_stream_refs={"commands": [{"stdout": stdout.stream_id, "stderr": stderr.stream_id}]},
            started_at=now,
        )
        await ValidationRunRepository(session).finish(
            run.id,
            status="succeeded",
            reason_code="VALIDATION_OK",
            finished_at=now + timedelta(minutes=2),
            coverage={
                "percent": 99.1,
                "minimum_percent": 99.0,
                "status": "succeeded",
                "reason_code": "COVERAGE_OK",
                "gaps": [{"path": "src/awf/mcp/server.py"}],
            },
        )
        await session.commit()
    return workspace_id


async def _seed_stale_reasons(factory: async_sessionmaker[AsyncSession]) -> str:
    workspace_id = await _seed_merge_queue(factory)
    async with factory() as session:
        candidate = (
            await MergeCandidateRepository(session).get_open_for_workspace_with_merge_inputs(
                workspace_id
            )
        )
        assert candidate is not None
        await StaleReasonRepository(session).replace_active_findings(
            workspace_id=workspace_id,
            candidate_id=candidate.id,
            attempt_id=candidate.attempt_id,
            task_id=candidate.task_id,
            findings=[],
        )
        await StaleReasonRepository(session).replace_active_findings(
            workspace_id=workspace_id,
            candidate_id=candidate.id,
            attempt_id=candidate.attempt_id,
            task_id=candidate.task_id,
            findings=[
                StaleReasonCreate(
                    reason_code="STALE_SCHEMA",
                    trigger_type="schema_changed",
                    trigger_ref="migrations/versions/new.py",
                    explanation="Schema lineage changed on target branch.",
                )
            ],
        )
        await session.commit()
    return workspace_id


async def _seed_operation(factory: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    workspace_id = await _workspace(factory, title="Operation parity")
    async with factory() as session:
        operation = await OperationRepository(session).create(
            workspace_id=workspace_id,
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
        await OperationRepository(session).finish(
            operation,
            status=OperationStatus.succeeded,
            result={
                "status": "validated",
                "log_stream_refs": {"validation": "validation.01_validate.stdout"},
            },
        )
        await session.commit()
        return workspace_id, operation.id


def _normalize_metric_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.pop("generated_at", None)
    normalized.pop("window_start", None)
    return normalized


def _string_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "string":
        return schema
    for candidate in schema.get("anyOf", []):
        if candidate.get("type") == "string":
            return candidate
    raise AssertionError(f"No string schema in {schema!r}")


def _array_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "array":
        return schema
    for candidate in schema.get("anyOf", []):
        if candidate.get("type") == "array":
            return candidate
    raise AssertionError(f"No array schema in {schema!r}")


class TestMcpOperatorSurfaceRegistration:
    @pytest.mark.unit
    async def test_operator_parity_tools_registered(self, operator_stack: OperatorStack) -> None:
        tools = await operator_stack.mcp.list_tools()
        by_name = {tool.name: tool for tool in tools}

        assert set(by_name) >= NEW_OPERATOR_TOOLS
        for name in NEW_OPERATOR_TOOLS:
            description = (by_name[name].description or "").lower()
            assert "read-only" in description
            assert "operator" in description
            assert "shell" not in description
            assert "docker exec" not in description

    @pytest.mark.unit
    async def test_read_only_operator_tool_schemas_are_bounded_and_non_executing(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        tools = {tool.name: tool for tool in await operator_stack.mcp.list_tools()}

        forbidden_names = [
            name
            for name in tools
            if name.startswith(FORBIDDEN_OPERATOR_TOOL_PREFIXES)
            or name == "awf_list_workspace_secret_leases"
        ]
        assert forbidden_names == []

        for name in NEW_OPERATOR_TOOLS:
            props = tools[name].inputSchema.get("properties", {})
            assert FORBIDDEN_READ_ONLY_INPUTS.isdisjoint(props), name
            if "cursor" in props:
                assert _string_schema(props["cursor"])["maxLength"] <= 256

        for name in BOUNDED_READ_ONLY_LIST_TOOLS:
            props = tools[name].inputSchema.get("properties", {})
            assert "limit" in props, name
            assert props["limit"]["minimum"] == 1
            assert props["limit"]["maximum"] <= 500

        readiness_props = tools["awf_get_service_readiness"].inputSchema["properties"]
        providers_schema = _array_schema(readiness_props["providers"])
        assert providers_schema["maxItems"] <= 16
        provider_item_schema = providers_schema["items"]
        assert provider_item_schema["minLength"] == 1
        assert provider_item_schema["maxLength"] <= 64


class TestMcpOperatorSurfaceParity:
    @pytest.mark.unit
    async def test_core_release_readiness_tool_matches_rest_payload(
        self,
        operator_stack: OperatorStack,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import awf.api.routes.health as health_route
        import awf.service.readiness as readiness_module

        async def _collect(**_kwargs: object) -> CoreReadinessReport:
            return CoreReadinessReport(
                status="ok",
                checks=(
                    CoreReadinessCheck(
                        name="prd_slo_thresholds",
                        status="ok",
                        reason_code="PRD_SLO_THRESHOLDS_MET",
                        message="rolling PRD SLO thresholds meet Core release criteria",
                        evidence={"since_hours": 168},
                    ),
                ),
                next_actions=(),
            )

        monkeypatch.setattr(health_route, "collect_core_readiness_report", _collect)
        monkeypatch.setattr(readiness_module, "collect_core_readiness_report", _collect)

        rest_response = await operator_stack.client.get(
            "/release-readiness",
            params={"failure_window_hours": 12, "slo_window_hours": 168},
        )
        assert rest_response.status_code == 200

        mcp = await _call(
            operator_stack.mcp,
            "awf_get_core_release_readiness",
            {"failure_window_hours": 12, "slo_window_hours": 168},
        )

        assert mcp == rest_response.json()

    @pytest.mark.unit
    async def test_missing_read_only_resources_return_null_tool_results(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        for tool_name, args in (
            ("awf_list_workspace_validation", {"workspace_id": "ws_missing"}),
            ("awf_list_workspace_stale_reasons", {"workspace_id": "ws_missing"}),
            ("awf_list_workspace_artifacts", {"workspace_id": "ws_missing"}),
            ("awf_get_operation", {"operation_id": "op_missing"}),
        ):
            result = await _call_result(operator_stack.mcp, tool_name, args)
            assert result.isError is False
            assert result.structuredContent is None

    @pytest.mark.unit
    async def test_empty_read_only_operator_surfaces_match_rest_payloads(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        list_cases: list[tuple[str, str, dict[str, Any], str, dict[str, Any]]] = [
            ("merge_queue", "/v1/merge-queue", {"limit": 10}, "awf_list_merge_queue", {"limit": 10}),
            (
                "workspace_overview",
                "/v1/workspaces/overview",
                {"limit": 10},
                "awf_list_workspace_overview",
                {"limit": 10},
            ),
            ("tasks", "/v1/tasks", {"limit": 10}, "awf_list_tasks", {"limit": 10}),
            ("locks", "/v1/locks", {"limit": 10}, "awf_list_locks", {"limit": 10}),
        ]
        empty_payloads: dict[str, dict[str, Any]] = {}

        for label, path, params, tool_name, args in list_cases:
            response = await resource_stack.client.get(
                path,
                params=params,
                headers=resource_stack.auth_headers,
            )
            assert response.status_code == 200
            rest = response.json()
            mcp = await _call(resource_stack.mcp, tool_name, args)

            assert mcp == rest
            assert rest["items"] == []
            empty_payloads[label] = rest

        overlap_response = await resource_stack.client.get(
            "/v1/locks/overlap-graph",
            params={"limit": 10},
            headers=resource_stack.auth_headers,
        )
        assert overlap_response.status_code == 200
        overlap_rest = overlap_response.json()
        overlap_mcp = await _call(
            resource_stack.mcp,
            "awf_get_overlap_graph",
            {"limit": 10},
        )
        assert overlap_mcp == overlap_rest
        assert overlap_rest["nodes"] == []
        assert overlap_rest["edges"] == []

        metric_cases: list[tuple[str, str, dict[str, Any], str, dict[str, Any]]] = [
            (
                "failures",
                "/v1/metrics/failures/summary",
                {"since_hours": 2, "limit": 5},
                "awf_get_failure_analysis_summary",
                {"since_hours": 2, "limit": 5},
            ),
            (
                "reliability",
                "/v1/metrics/workspaces/summary",
                {"since_hours": 2},
                "awf_get_workspace_reliability_summary",
                {"since_hours": 2},
            ),
            (
                "resources",
                "/v1/metrics/resources/saturation",
                {},
                "awf_get_resource_saturation_summary",
                {},
            ),
            (
                "slo",
                "/v1/metrics/slo",
                {"since_hours": 2},
                "awf_get_slo_metrics_summary",
                {"since_hours": 2},
            ),
        ]
        metric_payloads: dict[str, dict[str, Any]] = {}

        for label, path, params, tool_name, args in metric_cases:
            response = await resource_stack.client.get(
                path,
                params=params,
                headers=resource_stack.auth_headers,
            )
            assert response.status_code == 200
            rest = response.json()
            mcp = await _call(resource_stack.mcp, tool_name, args)
            assert isinstance(mcp, dict)

            assert _normalize_metric_payload(mcp) == _normalize_metric_payload(rest)
            metric_payloads[label] = rest

        assert empty_payloads["merge_queue"]["has_more"] is False
        assert empty_payloads["workspace_overview"]["has_more"] is False
        assert empty_payloads["tasks"]["has_more"] is False
        assert empty_payloads["locks"]["has_more"] is False
        assert metric_payloads["failures"]["total_failed_workspaces"] == 0
        assert metric_payloads["failures"]["failure_groups"] == []
        assert metric_payloads["failures"]["latest_examples"] == []
        assert metric_payloads["reliability"]["active_count"] == 0
        assert metric_payloads["reliability"]["failed_count"] == 0
        assert metric_payloads["resources"]["workspace_counts"]["active_total"] == 0
        assert metric_payloads["resources"]["reserved_resources"]["active_workspace_count"] == 0
        assert metric_payloads["slo"]["creation_total"] == 0
        assert metric_payloads["slo"]["cleanup_total"] == 0

    @pytest.mark.unit
    async def test_service_operator_surfaces_redact_token_values(
        self,
        engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        api_secret = "api-secret-do-not-leak-12345"
        provider_secret = "ghp_providerSecretDoNotLeak12345"
        work_dir = tmp_path / "redacted-awf-state"
        factory = make_session_factory(engine)
        settings = Settings(
            _env_file=None,
            api_token=api_secret,
            github_token=provider_secret,
            work_dir=str(work_dir),
            min_free_disk_bytes=700,
            worker_max_concurrent_provisions=5,
            worker_max_concurrent_executions=2,
        )

        def leaky_readiness(
            _settings: Settings,
            *,
            validated_strict_providers: set[Any] | None = None,
        ) -> dict[str, Any]:
            return {
                "service": "awf",
                "version": "test",
                "status": "ok",
                "checks": {
                    "db": {
                        "ok": True,
                        "status": "ok",
                        "reason": None,
                        "detail": api_secret,
                    }
                },
                "agent_readiness": {
                    "status": "ok",
                    "providers": {
                        "github": {
                            "status": "ok",
                            "detail": provider_secret,
                        }
                    },
                    "strict_providers": sorted(validated_strict_providers or ()),
                },
            }

        def leaky_health() -> dict[str, Any]:
            return {
                "status": "ok",
                "service": "awf",
                "version": f"test {api_secret}",
            }

        def leaky_disk(current_settings: Settings) -> DiskCheck:
            base = _ok_disk_check(current_settings)
            return DiskCheck(
                path=base.path,
                checked_path=base.checked_path,
                total_bytes=base.total_bytes,
                used_bytes=base.used_bytes,
                free_bytes=base.free_bytes,
                percent_free=base.percent_free,
                threshold_bytes=base.threshold_bytes,
                ok=base.ok,
                status=base.status,
                reason=base.reason,
                detail=provider_secret,
            )

        mcp = build_mcp_server(
            service=WorkspaceService(factory, settings=settings),
            settings=settings,
            disk_check_provider=leaky_disk,
            orphan_resource_summary_provider=_no_orphan_summary,
            runtime_health_summary_provider=_empty_runtime_health_summary,
            readiness_provider=leaky_readiness,
            health_provider=leaky_health,
        )

        for tool_name in (
            "awf_get_service_readiness",
            "awf_get_service_health",
            "awf_get_resource_saturation_summary",
        ):
            payload = await _call(mcp, tool_name, {})
            rendered = json.dumps(payload, sort_keys=True)

            assert api_secret not in rendered
            assert provider_secret not in rendered
            assert "<redacted>" in rendered

    @pytest.mark.unit
    async def test_resource_provider_helpers_support_async_and_absent_providers(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        async def async_disk(settings: Settings) -> DiskCheck:
            return _ok_disk_check(settings)

        async def async_orphan(settings: Settings, session: AsyncSession) -> Any:
            return _no_orphan_summary(settings, session)

        async def async_runtime(
            settings: Settings,
            session: AsyncSession,
            orphan_resources: Any,
        ) -> WorkspaceRuntimeHealthSummary:
            return _empty_runtime_health_summary(settings, session, orphan_resources)

        async with resource_stack.factory() as session:
            assert (
                await mcp_server._provided_disk_check(
                    disk_check_provider=None,
                    settings=resource_stack.settings,
                )
                is None
            )
            disk_check = await mcp_server._provided_disk_check(
                disk_check_provider=async_disk,
                settings=resource_stack.settings,
            )
            assert disk_check is not None
            assert disk_check.reason == "SUFFICIENT_DISK"

            assert (
                await mcp_server._provided_orphan_resources(
                    orphan_resource_summary_provider=None,
                    settings=resource_stack.settings,
                    session=session,
                )
                is None
            )
            orphan_resources = await mcp_server._provided_orphan_resources(
                orphan_resource_summary_provider=async_orphan,
                settings=resource_stack.settings,
                session=session,
            )
            assert orphan_resources is not None
            assert orphan_resources.reason == "NO_ORPHANS"

            assert (
                await mcp_server._provided_runtime_health(
                    runtime_health_summary_provider=None,
                    settings=resource_stack.settings,
                    session=session,
                    orphan_resources=orphan_resources,
                )
                is None
            )
            assert (
                await mcp_server._provided_runtime_health(
                    runtime_health_summary_provider=async_runtime,
                    settings=resource_stack.settings,
                    session=session,
                    orphan_resources=None,
                )
                is None
            )
            runtime_health = await mcp_server._provided_runtime_health(
                runtime_health_summary_provider=async_runtime,
                settings=resource_stack.settings,
                session=session,
                orphan_resources=orphan_resources,
            )
            assert runtime_health is not None
            assert runtime_health.stranded_count == 0

    @pytest.mark.unit
    async def test_readiness_and_health_provider_helpers_support_sync_async_and_absent(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        expected_readiness = {
            "service": "awf",
            "version": "test",
            "status": "ok",
            "checks": {},
            "agent_readiness": {"status": "ok"},
        }
        expected_health = {
            "status": "ok",
            "service": "awf",
            "version": "test",
        }

        def sync_readiness(
            settings: Settings,
            *,
            validated_strict_providers: set[Any] | None = None,
        ) -> dict[str, Any]:
            return expected_readiness

        async def async_readiness(
            settings: Settings,
            *,
            validated_strict_providers: set[Any] | None = None,
        ) -> dict[str, Any]:
            return expected_readiness

        def sync_health() -> dict[str, Any]:
            return expected_health

        async def async_health() -> dict[str, Any]:
            return expected_health

        sync_result = await mcp_server._provided_readiness(
            readiness_provider=sync_readiness,
            settings=resource_stack.settings,
        )
        assert sync_result == expected_readiness

        async_result = await mcp_server._provided_readiness(
            readiness_provider=async_readiness,
            settings=resource_stack.settings,
        )
        assert async_result == expected_readiness

        fallback_result = await mcp_server._provided_readiness(
            readiness_provider=None,
            settings=resource_stack.settings,
            session_factory=resource_stack.factory,
        )
        assert fallback_result["service"] == "awf"
        assert fallback_result["status"] in {"ok", "fail"}
        assert "checks" in fallback_result
        assert fallback_result["checks"]["db"]["ok"] is True
        assert fallback_result["checks"]["db"]["status"] == "ok"
        for key in (
            "docker_cli",
            "docker_daemon",
            "docker_compose",
            "agent_runtime_image",
        ):
            assert key in fallback_result["checks"]
            reason = fallback_result["checks"][key].get("reason")
            assert reason is not None or fallback_result["checks"][key]["ok"] is True
            if not fallback_result["checks"][key]["ok"]:
                assert reason != "PROVIDER_NOT_CONFIGURED"

        sync_health_result = await mcp_server._provided_health(
            health_provider=sync_health,
        )
        assert sync_health_result == expected_health

        async_health_result = await mcp_server._provided_health(
            health_provider=async_health,
        )
        assert async_health_result == expected_health

        fallback_health = await mcp_server._provided_health(
            health_provider=None,
        )
        assert fallback_health["status"] == "ok"
        assert fallback_health["service"] == "awf"

    @pytest.mark.unit
    async def test_readiness_fallback_runs_real_db_check(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        fallback_result = await mcp_server._provided_readiness(
            readiness_provider=None,
            settings=resource_stack.settings,
            session_factory=resource_stack.factory,
        )
        assert fallback_result["checks"]["db"]["ok"] is True
        assert fallback_result["checks"]["db"]["status"] == "ok"
        assert fallback_result["checks"]["db"]["reason"] is None

    @pytest.mark.unit
    async def test_readiness_fallback_reports_db_failure_when_no_session_factory(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        fallback_result = await mcp_server._provided_readiness(
            readiness_provider=None,
            settings=resource_stack.settings,
            session_factory=None,
        )
        assert fallback_result["checks"]["db"]["ok"] is False
        assert fallback_result["checks"]["db"]["status"] == "fail"
        assert fallback_result["checks"]["db"]["reason"] == "DB_NOT_CONFIGURED"
        assert fallback_result["status"] == "fail"

    @pytest.mark.unit
    async def test_readiness_fallback_with_strict_providers(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        from awf.service.provider_readiness import validate_provider_names

        strict = validate_provider_names(["github"])
        fallback_result = await mcp_server._provided_readiness(
            readiness_provider=None,
            settings=resource_stack.settings,
            session_factory=resource_stack.factory,
            validated_strict_providers=strict,
        )
        assert fallback_result["service"] == "awf"
        assert "agent_readiness" in fallback_result
        assert "github" in fallback_result["agent_readiness"]["strict_providers"]

    @pytest.mark.unit
    async def test_readiness_fallback_omits_strict_providers_when_none(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        fallback_result = await mcp_server._provided_readiness(
            readiness_provider=None,
            settings=resource_stack.settings,
            session_factory=resource_stack.factory,
        )
        assert fallback_result["agent_readiness"]["strict_providers"] == []

    @pytest.mark.unit
    async def test_readiness_provider_receives_strict_providers(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        from awf.service.provider_readiness import validate_provider_names

        captured: dict[str, Any] = {}
        strict = validate_provider_names(["github"])

        def capturing_readiness(
            settings: Settings,
            *,
            validated_strict_providers: set[Any] | None = None,
        ) -> dict[str, Any]:
            captured["providers"] = validated_strict_providers
            return {"service": "awf", "status": "ok", "checks": {}}

        result = await mcp_server._provided_readiness(
            readiness_provider=capturing_readiness,
            settings=resource_stack.settings,
            validated_strict_providers=strict,
        )
        assert result["status"] == "ok"
        assert captured["providers"] == strict

    @pytest.mark.unit
    async def test_read_only_operator_tools_use_shared_services_not_route_handlers(
        self,
        resource_stack: OperatorStack,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.api.routes import artifacts as artifact_routes
        from awf.api.routes import merge_queue as merge_queue_routes
        from awf.api.routes import metrics as metrics_routes
        from awf.api.routes import validation as validation_routes
        from awf.api.routes import workspaces as workspace_routes

        merge_workspace_id = await _seed_merge_queue(resource_stack.factory)
        validation_workspace_id = await _seed_validation(resource_stack.factory)
        artifact_workspace_id = await _workspace(
            resource_stack.factory,
            title="Artifact service parity",
        )

        async def fail_route_handler(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("MCP read-only parity tools must use shared services")

        monkeypatch.setattr(
            merge_queue_routes,
            "list_merge_queue",
            fail_route_handler,
        )
        monkeypatch.setattr(
            workspace_routes,
            "list_workspace_overview",
            fail_route_handler,
        )
        monkeypatch.setattr(
            workspace_routes,
            "list_workspace_stale_reasons",
            fail_route_handler,
        )
        monkeypatch.setattr(
            validation_routes,
            "list_validation_provenance",
            fail_route_handler,
        )
        monkeypatch.setattr(
            artifact_routes,
            "list_workspace_artifacts",
            fail_route_handler,
        )
        monkeypatch.setattr(
            metrics_routes,
            "get_failure_analysis_summary",
            fail_route_handler,
        )
        monkeypatch.setattr(
            metrics_routes,
            "get_workspace_reliability_summary",
            fail_route_handler,
        )
        monkeypatch.setattr(
            metrics_routes,
            "get_resource_saturation_summary",
            fail_route_handler,
        )
        monkeypatch.setattr(
            metrics_routes,
            "get_slo_metrics_summary",
            fail_route_handler,
        )

        tool_calls: list[tuple[str, dict[str, object]]] = [
            ("awf_list_merge_queue", {"limit": 10}),
            ("awf_list_workspace_overview", {"limit": 10}),
            ("awf_list_workspace_validation", {"workspace_id": validation_workspace_id}),
            ("awf_list_workspace_stale_reasons", {"workspace_id": merge_workspace_id}),
            ("awf_list_workspace_artifacts", {"workspace_id": artifact_workspace_id}),
            ("awf_get_failure_analysis_summary", {"since_hours": 2, "limit": 5}),
            ("awf_get_workspace_reliability_summary", {"since_hours": 2}),
            ("awf_get_resource_saturation_summary", {}),
            ("awf_get_slo_metrics_summary", {"since_hours": 2}),
        ]

        for tool_name, args in tool_calls:
            payload = await _call(resource_stack.mcp, tool_name, args)
            assert isinstance(payload, dict)

    @pytest.mark.unit
    async def test_merge_queue_tool_matches_rest_payload_and_reason_codes(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _seed_merge_queue(operator_stack.factory)

        response = await operator_stack.client.get(
            "/v1/merge-queue",
            params={"repo_url": "git@github.com:example/merge.git", "limit": 10},
        )
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            operator_stack.mcp,
            "awf_list_merge_queue",
            {"repo_url": "git@github.com:example/merge.git", "limit": 10},
        )

        assert mcp == rest
        item = next(item for item in rest["items"] if item["workspace_id"] == workspace_id)
        assert item["merge_blocker_reason"] == "stale"
        assert item["required_next_action"] == "rebase"
        assert item["validation_freshness_status"] == "fresh"
        assert item["validation_reason_code"] == "validation_fresh"
        assert [reason["reason_code"] for reason in item["stale_reasons"]] == [
            "STALE_DEPENDENCY"
        ]

    @pytest.mark.unit
    async def test_workspace_overview_tool_matches_rest_payload(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _workspace(
            operator_stack.factory,
            title="Overview parity",
            status=WorkspaceStatus.running,
            task_policy={"agent_model": "gpt-5.3-codex", "agent_effort": "high"},
            resolved_profile={
                "name": "operator-open",
                "security": {"egress": {"mode": "open"}},
            },
        )
        async with operator_stack.factory() as session:
            await OperationRepository(session).create(
                workspace_id=workspace_id,
                operation_type=OperationType.validate,
                status=OperationStatus.pending,
                payload={"source": "pr_monitor"},
            )
            await session.commit()

        response = await operator_stack.client.get(
            "/v1/workspaces/overview",
            params={"status": "running"},
        )
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            operator_stack.mcp,
            "awf_list_workspace_overview",
            {"workspace_status": "running"},
        )

        assert mcp == rest
        item = next(item for item in rest["items"] if item["workspace_id"] == workspace_id)
        assert item["current_phase"] == "running"
        assert item["active_operation"] == "validate"
        assert item["last_event"]["reason_code"] == "TEST_EVENT"
        assert item["agent_model"] == "gpt-5.3-codex"
        assert item["network_posture"] == "open"

    @pytest.mark.unit
    async def test_validation_provenance_tool_matches_rest_payload(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _seed_validation(operator_stack.factory)

        response = await operator_stack.client.get(f"/v1/workspaces/{workspace_id}/validation")
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            operator_stack.mcp,
            "awf_list_workspace_validation",
            {"workspace_id": workspace_id},
        )

        assert mcp == rest
        item = rest["items"][0]
        assert item["tier"] == 2
        assert item["command_set_hash"]
        assert item["profile_name"] == "operator-profile"
        assert item["coverage_status"] == "succeeded"
        assert item["stream_ids"] == {
            "stdout": "validation.01_validate.stdout",
            "stderr": "validation.01_validate.stderr",
        }

        limited_response = await operator_stack.client.get(
            f"/v1/workspaces/{workspace_id}/validation",
            params={"limit": 1},
        )
        limited_mcp = await _call(
            operator_stack.mcp,
            "awf_list_workspace_validation",
            {"workspace_id": workspace_id, "limit": 1},
        )

        assert limited_response.status_code == 200
        assert limited_mcp == limited_response.json()
        assert limited_response.json()["limit"] == 1

    @pytest.mark.unit
    async def test_stale_reasons_tool_matches_rest_active_and_resolved_payloads(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _seed_stale_reasons(operator_stack.factory)

        active_response = await operator_stack.client.get(
            f"/v1/workspaces/{workspace_id}/stale-reasons"
        )
        resolved_response = await operator_stack.client.get(
            f"/v1/workspaces/{workspace_id}/stale-reasons",
            params={"include_resolved": "true"},
        )

        assert active_response.status_code == 200
        assert resolved_response.status_code == 200
        active_mcp = await _call(
            operator_stack.mcp,
            "awf_list_workspace_stale_reasons",
            {"workspace_id": workspace_id},
        )
        resolved_mcp = await _call(
            operator_stack.mcp,
            "awf_list_workspace_stale_reasons",
            {"workspace_id": workspace_id, "include_resolved": True},
        )

        assert active_mcp == active_response.json()
        assert resolved_mcp == resolved_response.json()
        assert [item["status"] for item in active_response.json()["items"]] == ["active"]
        assert {item["status"] for item in resolved_response.json()["items"]} == {
            "active",
            "resolved",
        }

        limited_response = await operator_stack.client.get(
            f"/v1/workspaces/{workspace_id}/stale-reasons",
            params={"include_resolved": "true", "limit": 1},
        )
        limited_mcp = await _call(
            operator_stack.mcp,
            "awf_list_workspace_stale_reasons",
            {"workspace_id": workspace_id, "include_resolved": True, "limit": 1},
        )

        assert limited_response.status_code == 200
        assert limited_mcp == limited_response.json()
        assert len(limited_response.json()["items"]) == 1
        assert limited_response.json()["has_more"] is True

    @pytest.mark.unit
    async def test_artifacts_tool_matches_rest_metadata_payload(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _workspace(operator_stack.factory, title="Artifact parity")
        artifact_dir = Path(operator_stack.settings.work_dir) / "artifacts" / workspace_id
        nested = artifact_dir / "logs"
        nested.mkdir(parents=True)
        stdout = nested / "stdout.txt"
        stdout.write_text("alpha\n", encoding="utf-8")
        screenshot = artifact_dir / "screenshot.png"
        screenshot.write_bytes(b"\x89PNG\r\n")
        outside = artifact_dir.parent / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        (artifact_dir / "outside-link.txt").symlink_to(outside)

        response = await operator_stack.client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            headers=operator_stack.auth_headers,
        )
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            operator_stack.mcp,
            "awf_list_workspace_artifacts",
            {"workspace_id": workspace_id},
        )

        assert mcp == rest
        assert [item["relative_path"] for item in rest["items"]] == [
            "logs/stdout.txt",
            "screenshot.png",
        ]
        assert "data" not in rest["items"][0]
        assert "content" not in rest["items"][0]

        limited_response = await operator_stack.client.get(
            f"/v1/workspaces/{workspace_id}/artifacts",
            params={"limit": 1},
            headers=operator_stack.auth_headers,
        )
        limited_mcp = await _call(
            operator_stack.mcp,
            "awf_list_workspace_artifacts",
            {"workspace_id": workspace_id, "limit": 1},
        )

        assert limited_response.status_code == 200
        assert limited_mcp == limited_response.json()
        assert len(limited_response.json()["items"]) == 1
        assert limited_response.json()["has_more"] is True

    @pytest.mark.unit
    async def test_failure_analysis_metrics_tool_matches_rest_payload(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        now = datetime.now(UTC)
        validation_id = await _workspace(
            operator_stack.factory,
            title="Validation failure",
            status=WorkspaceStatus.failed,
            updated_at=now - timedelta(minutes=5),
            failure_reason=FailureReason.validation_failure,
            failure_message="pytest failed",
            task_policy={"agent_model": "gpt-5.3-codex"},
        )
        await _workspace(
            operator_stack.factory,
            title="Cancelled failure",
            status=WorkspaceStatus.cancelled,
            updated_at=now - timedelta(minutes=4),
            failure_reason=FailureReason.agent_failure,
        )

        response = await operator_stack.client.get(
            "/v1/metrics/failures/summary",
            params={"since_hours": 2, "limit": 5},
        )
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            operator_stack.mcp,
            "awf_get_failure_analysis_summary",
            {"since_hours": 2, "limit": 5},
        )

        assert _normalize_metric_payload(mcp) == _normalize_metric_payload(rest)  # type: ignore[arg-type]
        assert rest["total_failed_workspaces"] == 1
        assert rest["latest_examples"][0]["workspace_id"] == validation_id
        assert rest["failure_groups"][0]["failure_reason"] == "validation_failure"
        assert rest["root_cause_clusters"][0]["sample_workspace_ids"] == [validation_id]

    @pytest.mark.unit
    async def test_operations_tool_matches_rest_filters_and_detail(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id, operation_id = await _seed_operation(operator_stack.factory)

        list_response = await operator_stack.client.get(
            "/v1/operations",
            params={
                "workspace_id": workspace_id,
                "type": "validate",
                "status": "succeeded",
                "limit": 10,
            },
        )
        detail_response = await operator_stack.client.get(f"/v1/operations/{operation_id}")
        assert list_response.status_code == 200
        assert detail_response.status_code == 200

        list_mcp = await _call(
            operator_stack.mcp,
            "awf_list_operations",
            {
                "workspace_id": workspace_id,
                "operation_type": "validate",
                "status": "succeeded",
                "limit": 10,
            },
        )
        detail_mcp = await _call(
            operator_stack.mcp,
            "awf_get_operation",
            {"operation_id": operation_id},
        )

        assert list_mcp == list_response.json()
        assert detail_mcp == detail_response.json()
        assert detail_response.json()["reason_code"] == "OPERATOR_VALIDATE"
        assert detail_response.json()["log_stream_ids"] == [
            "monitor.log",
            "validation.01_validate.stdout",
        ]

    @pytest.mark.unit
    async def test_workspace_reliability_and_slo_tools_match_rest_payloads(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        now = datetime.now(UTC)
        await _workspace(
            operator_stack.factory,
            title="Reliability failed",
            status=WorkspaceStatus.failed,
            updated_at=now - timedelta(minutes=5),
            failure_reason=FailureReason.agent_failure,
        )
        await _workspace(
            operator_stack.factory,
            title="Reliability completed",
            status=WorkspaceStatus.completed,
            updated_at=now - timedelta(minutes=4),
        )

        reliability_response = await operator_stack.client.get(
            "/v1/metrics/workspaces/summary",
            params={"since_hours": 2},
        )
        slo_response = await operator_stack.client.get(
            "/v1/metrics/slo",
            params={"since_hours": 2},
        )
        assert reliability_response.status_code == 200
        assert slo_response.status_code == 200

        reliability_mcp = await _call(
            operator_stack.mcp,
            "awf_get_workspace_reliability_summary",
            {"since_hours": 2},
        )
        slo_mcp = await _call(
            operator_stack.mcp,
            "awf_get_slo_metrics_summary",
            {"since_hours": 2},
        )

        assert _normalize_metric_payload(reliability_mcp) == _normalize_metric_payload(  # type: ignore[arg-type]
            reliability_response.json()
        )
        assert _normalize_metric_payload(slo_mcp) == _normalize_metric_payload(  # type: ignore[arg-type]
            slo_response.json()
        )
        assert reliability_response.json()["failed_count"] == 1
        assert slo_response.json()["creation_failed"] == 1

    @pytest.mark.unit
    async def test_resource_saturation_tool_matches_rest_payload_with_fake_providers(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        now = datetime.now(UTC)
        await _workspace(
            resource_stack.factory,
            title="Resource running",
            status=WorkspaceStatus.running,
            updated_at=now - timedelta(minutes=2),
        )

        response = await resource_stack.client.get("/v1/metrics/resources/saturation")
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            resource_stack.mcp,
            "awf_get_resource_saturation_summary",
            {},
        )

        assert _normalize_metric_payload(mcp) == _normalize_metric_payload(rest)  # type: ignore[arg-type]
        assert rest["disk"]["reason"] == "SUFFICIENT_DISK"
        assert rest["orphan_resources"]["reason"] == "NO_ORPHANS"
        assert rest["runtime_health"]["stranded_count"] == 0
        assert rest["admission"]["reason"] in {
            "ADMISSION_OK",
            "WORKER_EXECUTION_CONCURRENCY_SATURATED",
        }

    @pytest.mark.unit
    async def test_overlap_graph_tool_matches_rest_payload(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        running_id = await _workspace(
            operator_stack.factory,
            title="Running overlap",
            status=WorkspaceStatus.running,
            repo_url="git@github.com:example/overlap.git",
            task_class="refactor_task",
            owned_paths=["src/awf/service/**"],
        )
        queued_id = await _workspace(
            operator_stack.factory,
            title="Queued overlap",
            status=WorkspaceStatus.ready,
            repo_url="git@github.com:example/overlap.git",
            task_class="refactor_task",
            owned_paths=["src/awf/service/workspaces.py"],
        )

        response = await operator_stack.client.get(
            "/v1/locks/overlap-graph",
            params={
                "repo_url": "git@github.com:example/overlap.git",
                "task_class": "refactor_task",
                "queue_state": "all",
                "limit": 10,
            },
        )
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            operator_stack.mcp,
            "awf_get_overlap_graph",
            {
                "repo_url": "git@github.com:example/overlap.git",
                "task_class": "refactor_task",
                "queue_state": "all",
                "limit": 10,
            },
        )

        assert mcp == rest
        assert {node["workspace_id"] for node in rest["nodes"]} == {running_id, queued_id}
        assert rest["edges"][0]["reason_code"] == "OWNED_PATH_OVERLAP_RISK"
        assert rest["edges"][0]["blocks_launch"] is False
        assert rest["edges"][0]["path_matches"][0]["match_reason_code"] == (
            "OWNED_PATH_WILDCARD_MATCH"
        )

    @pytest.mark.unit
    async def test_invalid_cursors_return_structured_mcp_errors(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        merge_result = await _call_result(
            operator_stack.mcp,
            "awf_list_merge_queue",
            {"cursor": "not-a-cursor"},
        )
        overview_result = await _call_result(
            operator_stack.mcp,
            "awf_list_workspace_overview",
            {"cursor": "not-a-cursor"},
        )

        assert merge_result.isError is True
        assert merge_result.structuredContent == {
            "error_code": "INVALID_CURSOR",
            "message": "Invalid merge queue cursor.",
            "detail": None,
        }
        assert overview_result.isError is True
        assert overview_result.structuredContent == {
            "error_code": "INVALID_CURSOR",
            "message": "Invalid workspace overview cursor.",
            "detail": None,
        }

    @pytest.mark.unit
    async def test_task_listing_tool_matches_rest_payload(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _seed_merge_queue(operator_stack.factory)
        await _workspace(
            operator_stack.factory,
            title="Legacy workspace",
            repo_url="git@github.com:example/legacy.git",
            status=WorkspaceStatus.requested,
            task_class=None,
        )

        response = await operator_stack.client.get(
            "/v1/tasks",
            params={"limit": 20},
            headers=operator_stack.auth_headers,
        )
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            operator_stack.mcp,
            "awf_list_tasks",
            {"limit": 20},
        )

        assert mcp == rest
        item = next(item for item in rest["items"] if item["workspace_id"] == workspace_id)
        assert item["attempt_id"] is not None
        assert item["is_canonical_for_merge"] is True
        assert item["canonical_attempt_id"] is not None
        assert item["agent_model"] is not None
        legacy_item = next(
            item for item in rest["items"]
            if item.get("attempt_id") is None
        )
        assert legacy_item["task_id"] is not None

    @pytest.mark.unit
    async def test_task_listing_includes_attempt_without_merge_candidate(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
        async with operator_stack.factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.create(
                repo_url="git@github.com:example/no-candidate.git",
                branch_base="main",
                task_title="No candidate task",
                task_prompt="Test task without merge candidate.",
                task_external_id="NC-1",
                task_class="test_task",
                owned_paths=["src/**"],
                agent=AgentRuntime.codex.value,
                test_commands=["pytest -q"],
            )
            workspace.status = WorkspaceStatus.running.value
            workspace.created_at = now
            workspace.updated_at = now
            task = await TaskRepository(session).create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=workspace.task_external_id,
                idempotency_key=None,
                task_class=workspace.task_class,
                owned_paths=list(workspace.owned_paths),
            )
            await TaskAttemptRepository(session).create_for_workspace(
                task=task,
                workspace=workspace,
            )
            await session.commit()

        mcp_payload = await _call(
            operator_stack.mcp,
            "awf_list_tasks",
            {"limit": 20},
        )
        assert isinstance(mcp_payload, dict)
        items = mcp_payload["items"]
        no_candidate_item = next(
            item for item in items
            if item.get("candidate_id") is None and item.get("attempt_id") is not None
        )
        assert no_candidate_item["readiness"] is None

    @pytest.mark.unit
    async def test_task_attempts_tool_matches_rest_payload(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _seed_merge_queue(operator_stack.factory)
        async with operator_stack.factory() as session:
            attempt_repo = TaskAttemptRepository(session)
            attempt = await attempt_repo.get_by_workspace_id(workspace_id)
            assert attempt is not None
            task_id = attempt.task_id
            task = await TaskRepository(session).get(task_id)
            assert task is not None
            task_ref = task.external_id or task.id
            await session.commit()

        response = await operator_stack.client.get(
            f"/v1/tasks/{task_ref}/attempts",
            headers=operator_stack.auth_headers,
        )
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            operator_stack.mcp,
            "awf_list_task_attempts",
            {"task_ref": task_ref},
        )

        assert mcp == rest
        assert rest["task_id"] is not None
        assert rest["task_ref"] == task_ref
        assert len(rest["items"]) >= 1
        attempt = rest["items"][0]
        assert attempt["attempt_number"] == 1
        assert attempt["is_canonical_for_merge"] is True

    @pytest.mark.unit
    async def test_missing_task_attempts_return_structured_error(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        result = await _call_result(
            operator_stack.mcp,
            "awf_list_task_attempts",
            {"task_ref": "nonexistent"},
        )

        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "NOT_FOUND",
            "message": "No task with ref nonexistent",
            "detail": None,
        }

    @pytest.mark.unit
    async def test_locks_tool_matches_rest_payload(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        await _workspace(
            operator_stack.factory,
            title="Lock parity",
            status=WorkspaceStatus.monitoring_pr,
            repo_url="git@github.com:example/locks.git",
            task_class="refactor_task",
            owned_paths=["src/awf/mcp/**"],
        )

        response = await operator_stack.client.get(
            "/v1/locks",
            params={"repo_url": "git@github.com:example/locks.git", "limit": 10},
            headers=operator_stack.auth_headers,
        )
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            operator_stack.mcp,
            "awf_list_locks",
            {"repo_url": "git@github.com:example/locks.git", "limit": 10},
        )

        assert mcp == rest
        assert len(rest["items"]) >= 1
        lock_item = rest["items"][0]
        assert lock_item["owned_paths"] == ["src/awf/mcp/**"]
        assert lock_item["overlap_risks"] is not None

    @pytest.mark.unit
    async def test_locks_invalid_cursor_returns_structured_mcp_error(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        result = await _call_result(
            operator_stack.mcp,
            "awf_list_locks",
            {"cursor": "not-a-cursor"},
        )

        assert result.isError is True
        assert result.structuredContent == {
            "error_code": "INVALID_CURSOR",
            "message": "Invalid lock list cursor.",
            "detail": None,
        }

    @pytest.mark.unit
    async def test_service_health_tool_returns_healthz_payload(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        response = await operator_stack.client.get("/healthz")
        assert response.status_code == 200
        rest = response.json()
        mcp = await _call(
            operator_stack.mcp,
            "awf_get_service_health",
            {},
        )

        assert mcp == rest
        assert rest["status"] == "ok"
        assert rest["service"] == "awf"
        assert "version" in rest

    @pytest.mark.unit
    async def test_service_readiness_tool_matches_rest_payload(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        mcp_payload = await _call(
            resource_stack.mcp,
            "awf_get_service_readiness",
            {},
        )

        assert isinstance(mcp_payload, dict)
        assert mcp_payload["service"] == "awf"
        assert "version" in mcp_payload
        assert mcp_payload["status"] in {"ok", "degraded", "fail"}
        assert "checks" in mcp_payload
        assert "agent_readiness" in mcp_payload
        for check_name in ("db", "docker_cli", "docker_daemon", "docker_compose",
                           "agent_runtime_image", "orphan_resources"):
            assert check_name in mcp_payload["checks"]
            check = mcp_payload["checks"][check_name]
            assert "ok" in check
            assert "status" in check
            assert "reason" in check

    @pytest.mark.unit
    async def test_service_readiness_tool_with_providers_filter(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        mcp_payload = await _call(
            resource_stack.mcp,
            "awf_get_service_readiness",
            {"providers": ["github"]},
        )

        assert isinstance(mcp_payload, dict)
        assert mcp_payload["service"] == "awf"
        agent_readiness = mcp_payload["agent_readiness"]
        assert "strict_providers" in agent_readiness
        assert "github" in agent_readiness["strict_providers"]

    @pytest.mark.unit
    async def test_service_readiness_tool_with_invalid_providers_returns_error(
        self,
        resource_stack: OperatorStack,
    ) -> None:
        result = await _call_result(
            resource_stack.mcp,
            "awf_get_service_readiness",
            {"providers": ["nonexistent_provider"]},
        )

        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "INVALID_PROVIDERS"

    @pytest.mark.unit
    async def test_remonitor_workspace_tool_returns_control_response(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _workspace(
            operator_stack.factory,
            title="Remonitor parity",
            status=WorkspaceStatus.monitoring_pr,
        )

        result = await _call(
            operator_stack.mcp,
            "awf_remonitor_workspace",
            {"workspace_id": workspace_id, "reason": "PR monitor stuck"},
        )

        assert isinstance(result, dict)
        assert result["workspace_id"] == workspace_id
        assert result["operation_id"] is not None
        assert result["status"] == "monitoring_pr"

    @pytest.mark.unit
    async def test_remonitor_workspace_wrong_state_returns_structured_error(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _workspace(
            operator_stack.factory,
            title="Remonitor wrong state",
            status=WorkspaceStatus.requested,
        )

        result = await _call_result(
            operator_stack.mcp,
            "awf_remonitor_workspace",
            {"workspace_id": workspace_id},
        )

        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "WORKSPACE_STATE_NOT_REMONITORABLE"

    @pytest.mark.unit
    async def test_remonitor_workspace_with_idempotency_key_replays_on_duplicate(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _workspace(
            operator_stack.factory,
            title="Remonitor idempotency",
            status=WorkspaceStatus.monitoring_pr,
        )

        first = await _call(
            operator_stack.mcp,
            "awf_remonitor_workspace",
            {"workspace_id": workspace_id, "reason": "first call", "idempotency_key": "remonitor-mcp-1"},
        )
        assert isinstance(first, dict)
        first_op_id = first["operation_id"]

        second = await _call(
            operator_stack.mcp,
            "awf_remonitor_workspace",
            {"workspace_id": workspace_id, "reason": "first call", "idempotency_key": "remonitor-mcp-1"},
        )
        assert isinstance(second, dict)
        assert second["operation_id"] == first_op_id

        async with operator_stack.factory() as session:
            from awf.db.repositories import OperationRepository

            ops = await OperationRepository(session).list_for_workspace(
                workspace_id,
                operation_type=OperationType.remonitor,
            )
        remonitor_ops = [o for o in ops if o.idempotency_key == "remonitor-mcp-1"]
        assert len(remonitor_ops) == 1

    @pytest.mark.unit
    async def test_request_workspace_validation_tool_returns_operation_response(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _workspace(
            operator_stack.factory,
            title="Validate parity",
            status=WorkspaceStatus.monitoring_pr,
        )

        result = await _call(
            operator_stack.mcp,
            "awf_request_workspace_validation",
            {"workspace_id": workspace_id, "reason": "recheck validation", "requested_tier": 2},
        )

        assert isinstance(result, dict)
        assert result["workspace_id"] == workspace_id
        assert result["type"] == "validate"
        assert result["status"] == "pending"

    @pytest.mark.unit
    async def test_request_workspace_validation_wrong_state_returns_structured_error(
        self,
        operator_stack: OperatorStack,
    ) -> None:
        workspace_id = await _workspace(
            operator_stack.factory,
            title="Validate wrong state",
            status=WorkspaceStatus.requested,
        )

        result = await _call_result(
            operator_stack.mcp,
            "awf_request_workspace_validation",
            {"workspace_id": workspace_id},
        )

        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "WORKSPACE_STATE_NOT_VALIDATABLE"

    @pytest.mark.unit
    async def test_new_operator_tools_use_shared_services_not_route_handlers(
        self,
        resource_stack: OperatorStack,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.api.routes import health as health_routes
        from awf.api.routes import locks as lock_routes
        from awf.api.routes import tasks as task_routes

        await _seed_merge_queue(resource_stack.factory)

        async def fail_route_handler(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("MCP tools must use shared services, not route handlers")

        monkeypatch.setattr(
            task_routes,
            "list_tasks",
            fail_route_handler,
        )
        monkeypatch.setattr(
            task_routes,
            "list_task_attempts",
            fail_route_handler,
        )
        monkeypatch.setattr(
            lock_routes,
            "list_locks",
            fail_route_handler,
        )
        monkeypatch.setattr(
            health_routes,
            "healthz",
            fail_route_handler,
        )
        monkeypatch.setattr(
            health_routes,
            "readyz",
            fail_route_handler,
        )

        task_payload = await _call(
            resource_stack.mcp,
            "awf_list_tasks",
            {"limit": 10},
        )
        assert isinstance(task_payload, dict)
        assert "items" in task_payload

        health_payload = await _call(
            resource_stack.mcp,
            "awf_get_service_health",
            {},
        )
        assert isinstance(health_payload, dict)
        assert health_payload["status"] == "ok"
