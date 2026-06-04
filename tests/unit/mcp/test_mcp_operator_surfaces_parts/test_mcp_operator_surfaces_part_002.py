"""MCP parity tests for operator surfaces."""

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
from awf.service.resource_capacity import LocalCapacityLimits
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
    "awf_list_events",
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
    "awf_list_events",
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
    "token",
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
    app.state.local_capacity_detector = _local_capacity
    app.state.orphan_resource_summary_provider = _no_orphan_summary
    app.state.runtime_health_summary_provider = _empty_runtime_health_summary

    mcp = build_mcp_server(
        service=WorkspaceService(factory),
        settings=settings,
        disk_check_provider=_ok_disk_check,
        local_capacity_provider=_local_capacity,
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


def _local_capacity(_settings: Settings) -> LocalCapacityLimits:
    return LocalCapacityLimits(cpu_cores=8.0, memory_gb=24.0, source="test")


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
            log_stream_refs={
                "commands": [{"stdout": stdout.stream_id, "stderr": stderr.stream_id}]
            },
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
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
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


class TestMcpOperatorSurfaceParityPart001:
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
            headers=operator_stack.auth_headers,
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
            (
                "merge_queue",
                "/v1/merge-queue",
                {"limit": 10},
                "awf_list_merge_queue",
                {"limit": 10},
            ),
            (
                "workspace_overview",
                "/v1/workspaces/overview",
                {"limit": 10},
                "awf_list_workspace_overview",
                {"limit": 10},
            ),
            ("tasks", "/v1/tasks", {"limit": 10}, "awf_list_tasks", {"limit": 10}),
            ("locks", "/v1/locks", {"limit": 10}, "awf_list_locks", {"limit": 10}),
            ("global_events", "/v1/events", {"limit": 10}, "awf_list_events", {"limit": 10}),
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
        assert empty_payloads["global_events"]["has_more"] is False
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
    def test_sensitive_payload_redaction_resolves_service_settings_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.service import config as service_config

        api_secret = "api-secret-do-not-leak-12345"
        provider_secret = "ghp_providerSecretDoNotLeak12345"
        settings = Settings(
            _env_file=None,
            api_token=api_secret,
            github_token=provider_secret,
        )
        payload: dict[str, Any] = {
            "outer": [
                "plain text",
                {"inner": f"token {api_secret}", "provider": provider_secret},
            ],
            "list": ["another", {"deep": ["one", "two"]}],
        }
        calls = 0
        real_resolve = service_config.resolve_service_settings

        def counting_resolve(base: Settings | None = None) -> service_config.ServiceSettings:
            nonlocal calls
            calls += 1
            return real_resolve(base)

        monkeypatch.setattr(service_config, "resolve_service_settings", counting_resolve)

        redacted = mcp_server._redact_sensitive_payload(payload, settings)

        rendered = json.dumps(redacted, sort_keys=True)
        assert api_secret not in rendered
        assert provider_secret not in rendered
        assert calls == 1

    @pytest.mark.unit
    async def test_resource_provider_helpers_support_async_and_absent_providers(
        self,
        resource_stack: OperatorStack,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def async_disk(settings: Settings) -> DiskCheck:
            return _ok_disk_check(settings)

        async def async_local_capacity(settings: Settings) -> LocalCapacityLimits:
            return LocalCapacityLimits(cpu_cores=12.0, memory_gb=64.0, source="test")

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

            explicit_capacity = await mcp_server._provided_local_capacity(
                local_capacity_provider=None,
                settings=Settings(
                    _env_file=None,
                    local_capacity_cpu_cores=4.0,
                    local_capacity_memory_gb=8.0,
                ),
            )
            assert explicit_capacity == LocalCapacityLimits()
            local_capacity = await mcp_server._provided_local_capacity(
                local_capacity_provider=async_local_capacity,
                settings=resource_stack.settings,
            )
            assert local_capacity.cpu_cores == 12.0
            assert local_capacity.memory_gb == 64.0
            monkeypatch.setattr(
                mcp_server,
                "detect_local_capacity",
                lambda _settings: LocalCapacityLimits(
                    cpu_cores=16.0,
                    memory_gb=128.0,
                    source="docker",
                ),
            )
            detected_capacity = await mcp_server._provided_local_capacity(
                local_capacity_provider=None,
                settings=resource_stack.settings,
            )
            assert detected_capacity.cpu_cores == 16.0
            assert detected_capacity.memory_gb == 128.0

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
    async def test_readiness_fallback_propagates_auto_cleanup_orphans(
        self,
        resource_stack: OperatorStack,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The MCP readiness fallback must thread ``auto_cleanup_orphans`` into the
        shared health helper so MCP clients see the reaping posture (not the default
        dry-run-only summary) whenever ``AWF_AUTO_CLEANUP_ORPHANS`` is enabled."""
        import awf.api.routes.health as health_route

        captured: dict[str, Any] = {}
        real = health_route._check_orphan_resources_with_concurrent_scans

        async def _capture(**kwargs: Any) -> Any:
            captured["auto_cleanup_orphans"] = kwargs.get("auto_cleanup_orphans")
            return await real(**kwargs)

        monkeypatch.setattr(
            health_route,
            "_check_orphan_resources_with_concurrent_scans",
            _capture,
        )
        settings = resource_stack.settings.model_copy(update={"auto_cleanup_orphans": True})

        await mcp_server._provided_readiness(
            readiness_provider=None,
            settings=settings,
            session_factory=resource_stack.factory,
        )

        assert captured["auto_cleanup_orphans"] is True

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
