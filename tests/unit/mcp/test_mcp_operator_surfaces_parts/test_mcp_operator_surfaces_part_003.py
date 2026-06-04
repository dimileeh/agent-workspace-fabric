"""MCP parity tests for operator surfaces."""

from __future__ import annotations

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
from awf.mcp.server import WorkspaceService, build_mcp_server
from awf.service.disk import DiskCheck
from awf.service.orphan_resources import (
    WorkspaceIdView,
    build_orphan_resource_summary,
    empty_docker_scan,
    scan_managed_worktrees,
)
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


class TestMcpOperatorSurfaceParityPart002:
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
            headers=operator_stack.auth_headers,
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
            headers=operator_stack.auth_headers,
            params={
                "workspace_id": workspace_id,
                "type": "validate",
                "status": "succeeded",
                "limit": 10,
            },
        )
        detail_response = await operator_stack.client.get(
            f"/v1/operations/{operation_id}",
            headers=operator_stack.auth_headers,
        )
        workspace_response = await operator_stack.client.get(
            f"/v1/workspaces/{workspace_id}/operations",
            headers=operator_stack.auth_headers,
            params={
                "type": "validate",
                "status": "succeeded",
                "limit": 10,
            },
        )
        assert list_response.status_code == 200
        assert detail_response.status_code == 200
        assert workspace_response.status_code == 200

        list_mcp = await _call(
            operator_stack.mcp,
            "awf_list_operations",
            {
                "workspace_id": workspace_id,
                "type": "validate",
                "status": "succeeded",
                "limit": 10,
            },
        )
        workspace_mcp = await _call(
            operator_stack.mcp,
            "awf_list_workspace_operations",
            {
                "workspace_id": workspace_id,
                "type": "validate",
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
        assert workspace_mcp == workspace_response.json()
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
            headers=operator_stack.auth_headers,
        )
        slo_response = await operator_stack.client.get(
            "/v1/metrics/slo",
            params={"since_hours": 2},
            headers=operator_stack.auth_headers,
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

        response = await resource_stack.client.get(
            "/v1/metrics/resources/saturation",
            headers=resource_stack.auth_headers,
        )
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
            headers=operator_stack.auth_headers,
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
        operations_result = await _call_result(
            operator_stack.mcp,
            "awf_list_operations",
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
        assert operations_result.isError is True
        assert operations_result.structuredContent == {
            "error_code": "INVALID_CURSOR",
            "message": "Invalid operation list cursor.",
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
        legacy_item = next(item for item in rest["items"] if item.get("attempt_id") is None)
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
            item
            for item in items
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
        for check_name in (
            "db",
            "worker",
            "docker_cli",
            "docker_daemon",
            "docker_compose",
            "agent_runtime_image",
            "orphan_resources",
        ):
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
            {
                "workspace_id": workspace_id,
                "reason": "PR monitor stuck",
                "idempotency_key": "remonitor-parity",
            },
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
            {"workspace_id": workspace_id, "idempotency_key": "remonitor-wrong-state"},
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
            {
                "workspace_id": workspace_id,
                "reason": "first call",
                "idempotency_key": "remonitor-mcp-1",
            },
        )
        assert isinstance(first, dict)
        first_op_id = first["operation_id"]

        second = await _call(
            operator_stack.mcp,
            "awf_remonitor_workspace",
            {
                "workspace_id": workspace_id,
                "reason": "first call",
                "idempotency_key": "remonitor-mcp-1",
            },
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
            {
                "workspace_id": workspace_id,
                "reason": "recheck validation",
                "requested_tier": 2,
                "idempotency_key": "validate-parity",
            },
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
            {"workspace_id": workspace_id, "idempotency_key": "validate-wrong-state"},
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
