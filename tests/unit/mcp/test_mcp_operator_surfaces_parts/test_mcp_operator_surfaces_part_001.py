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
