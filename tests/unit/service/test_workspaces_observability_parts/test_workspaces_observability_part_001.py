"""Workspace service observability helpers."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.service.workspace_observability as workspace_observability_module
from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import (
    OperationRepository,
    OwnedPathOverlap,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.operations import build_operation_list_response
from awf.service.workspace_observability import (
    InvalidWorkspaceOverviewCursorError,
    _decode_overview_cursor,
    _json_safe_value,
    effective_agent_identity,
    workspace_pricing_metadata,
    workspace_recovery_summary,
    workspace_usage_summary,
)
from awf.service.workspaces import (
    WorkspaceRetryError,
    WorkspaceRetryNotAllowedError,
    WorkspaceRetryNotFoundError,
    WorkspaceService,
    _assert_supported_direct_create_task_kind,
    _effective_auto_merge,
    _parse_memory_gb,
    owned_path_overlap_warning_payload,
    owned_path_overlap_warnings,
    profile_with_requested_tier,
    retry_workspace_row,
    workspace_create_task_policy_snapshot,
)
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _lifecycle_event(
    *,
    event_type: str,
    occurred_at: datetime,
    old_state: str | None = None,
    new_state: str | None = None,
) -> object:
    return SimpleNamespace(
        event_type=event_type,
        old_state=old_state,
        new_state=new_state,
        reason_code="TEST",
        payload=None,
        occurred_at=occurred_at,
    )


def _recovery_event(
    *,
    event_type: str,
    occurred_at: datetime,
    old_state: str | None = None,
    new_state: str | None = None,
    reason_code: str | None = "RECOVERY_DISPATCH",
    payload: dict[str, object] | None = None,
    event_id: str = "evt_recovery",
) -> object:
    return SimpleNamespace(
        id=event_id,
        workspace_id="ws_recovery",
        event_type=event_type,
        old_state=old_state,
        new_state=new_state,
        reason_code=reason_code,
        payload=payload,
        occurred_at=occurred_at,
    )


def _recovery_operation(
    *,
    operation_id: str = "op_recovery",
    operation_type: str = OperationType.validate.value,
    status: str = OperationStatus.pending.value,
    created_at: datetime,
    payload: dict[str, object] | None = None,
    started_at: datetime | None = None,
) -> object:
    return SimpleNamespace(
        id=operation_id,
        workspace_id="ws_recovery",
        type=operation_type,
        status=status,
        payload=payload,
        created_at=created_at,
        started_at=started_at,
    )


def _workspace_for_lifecycle(
    *,
    status: WorkspaceStatus,
    created_at: datetime,
    events: list[object],
) -> object:
    return SimpleNamespace(
        id="ws_lifecycle",
        status=status.value,
        created_at=created_at,
        events=events,
    )


def _workspace_for_recovery(
    *,
    status: WorkspaceStatus = WorkspaceStatus.ready,
    created_at: datetime,
    events: list[object],
    operations: list[object] | None = None,
) -> object:
    return SimpleNamespace(
        id="ws_recovery",
        status=status.value,
        created_at=created_at,
        events=events,
        operations=operations or [],
    )


def _usage_snapshot(**overrides: object) -> object:
    from awf.service.usage_store import UsageSnapshot

    base: dict[str, object] = {
        "workspace_id": "ws_snap",
        "provider": "claude_code",
        "ccusage_source": "claude",
        "status": "available",
        "phase": "final",
        "captured_at": "2026-05-22T00:00:00+00:00",
    }
    base.update(overrides)
    return UsageSnapshot(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_workspace_overview_cursor_rejects_empty_workspace_id() -> None:
    cursor = base64.urlsafe_b64encode(
        json.dumps(
            {"t": "2026-04-29T12:00:00+00:00", "id": ""},
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")

    with pytest.raises(InvalidWorkspaceOverviewCursorError):
        _decode_overview_cursor(cursor)


@pytest.mark.unit
def test_json_safe_value_truncates_long_sequences() -> None:
    value = _json_safe_value(tuple(range(25)))

    assert value[-1] == "__truncated__"
    assert value[:3] == [0, 1, 2]


@pytest.mark.unit
def test_json_safe_value_keeps_short_sequences_unmarked() -> None:
    assert _json_safe_value([1, 2, 3]) == [1, 2, 3]


@pytest.mark.unit
def test_workspace_observability_private_fallbacks_cover_absent_policy_metadata() -> None:
    workspace = SimpleNamespace(resolved_profile=None)

    assert workspace_observability_module._overview_pricing_metadata(workspace) is None
    assert (
        workspace_observability_module._provider_readiness_preflight_from_task_policy(None) is None
    )


@pytest.mark.unit
def test_workspace_observability_handles_missing_activity_and_valid_pricing_metadata() -> None:
    stale_running = SimpleNamespace(
        status=WorkspaceStatus.running.value,
        last_activity_at=None,
        updated_at=None,
        created_at=None,
    )
    timestamp = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    priced_workspace = SimpleNamespace(
        id="ws_priced",
        resolved_profile={
            "pricing": {
                "pricing": {
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "currency": "USD",
                    "unit": "per_1k_tokens",
                    "price_per_unit": 0.01,
                    "timestamp": timestamp,
                }
            }
        },
    )
    malformed_pricing_workspace = SimpleNamespace(
        id="ws_bad_pricing_shape",
        resolved_profile={"pricing": {"pricing": "not-a-dict"}},
    )

    assert workspace_observability_module.is_workspace_stale_running(stale_running) is False
    pricing = workspace_observability_module._overview_pricing_metadata(priced_workspace)
    assert pricing is not None
    assert pricing["provider"] == "openai"
    assert pricing["model"] == "gpt-5.5"
    assert pricing["currency"] == "USD"
    assert pricing["unit"] == "per_1k_tokens"
    assert pricing["price_per_unit"] == 0.01
    assert pricing["timestamp"] == timestamp
    assert workspace_pricing_metadata(malformed_pricing_workspace) is None


@pytest.mark.unit
def test_workspace_usage_summary_handles_mixed_currencies_and_result_fallback() -> None:
    workspace = SimpleNamespace(
        operations=[
            SimpleNamespace(
                result={"usage": {"input_tokens": 1, "cost_estimate": 0.25, "currency": "USD"}},
                payload={},
            ),
            SimpleNamespace(
                result={},
                payload={"usage": {"output_tokens": 2, "cost_estimate": 0.50, "currency": "EUR"}},
            ),
        ]
    )

    summary = workspace_usage_summary(workspace)

    assert summary.status == "available"
    assert summary.input_tokens == 1
    assert summary.output_tokens == 2
    assert summary.currency == "MIXED"
    assert summary.cost_estimate is None


@pytest.mark.unit
def test_workspace_usage_summary_ignores_usage_dict_without_valid_metrics() -> None:
    workspace = SimpleNamespace(
        operations=[
            SimpleNamespace(
                result={
                    "usage": {
                        "input_tokens": True,
                        "output_tokens": False,
                        "total_tokens": None,
                        "cost_estimate": False,
                        "currency": "USD",
                    }
                },
                payload={},
            )
        ]
    )

    summary = workspace_usage_summary(workspace)

    assert summary.status == "unavailable"
    assert summary.reason == "usage_not_reported"
    assert summary.input_tokens is None
    assert summary.cost_estimate is None
    assert summary.currency is None


@pytest.mark.unit
def test_workspace_usage_summary_accumulates_same_currency_costs() -> None:
    workspace = SimpleNamespace(
        operations=[
            SimpleNamespace(
                result={"usage": {"input_tokens": 1, "cost_estimate": 0.25, "currency": "USD"}},
                payload={},
            ),
            SimpleNamespace(
                result={"usage": {"output_tokens": 2, "cost_estimate": 0.75, "currency": "USD"}},
                payload={},
            ),
        ]
    )

    summary = workspace_usage_summary(workspace)

    assert summary.status == "available"
    assert summary.currency == "USD"
    assert summary.cost_estimate == pytest.approx(1.0)


@pytest.mark.unit
def test_workspace_pricing_metadata_returns_none_for_invalid_payload() -> None:
    workspace = SimpleNamespace(
        resolved_profile={"pricing": {"pricing": {"provider": None, "model": "bad"}}}
    )

    assert workspace_pricing_metadata(workspace) is None


@pytest.mark.unit
async def test_workspace_service_round_trips_policy_metadata(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/service.git", "base_branch": "main"},
        task={
            "title": "Update dependency",
            "prompt": "Bump the dependency and adjust tests.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "task_class": "dependency_task",
            "owned_paths": ["pyproject.toml", ".github/workflows/publish.yml", "uv.lock"],
        },
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["uv run pytest -q"], "requested_tier": 1},
        resources={},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "observability test fixture",
        },
    )

    created = await service.create(request)
    fetched = await service.get(created.id)
    listed = await service.list(limit=10)

    assert created.task_class == "dependency_task"
    assert created.owned_paths == ["pyproject.toml", ".github/workflows/publish.yml", "uv.lock"]
    assert fetched is not None
    assert fetched.task_class == "dependency_task"
    assert fetched.owned_paths == ["pyproject.toml", ".github/workflows/publish.yml", "uv.lock"]
    assert listed[0].task_class == "dependency_task"
    assert listed[0].owned_paths == [
        "pyproject.toml",
        ".github/workflows/publish.yml",
        "uv.lock",
    ]


@pytest.mark.unit
async def test_workspace_service_create_v1_and_event_listing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    created = await service.create(
        WorkspaceCreateRequest(
            repo_url="git@github.com:example/v1.git",
            branch_base="main",
            task_title="Create v1 workspace",
            task_prompt="Exercise the v1 service path.",
            agent=AgentRuntime.codex,
            env_profile="python",
            test_commands=["pytest -q"],
            requires_database=True,
            preflight={
                "provider_readiness_override": True,
                "provider_readiness_override_reason": "observability test fixture",
            },
        )
    )

    fetched = await service.get(created.id)
    events = await service.list_events(created.id, event_type="workspace.created")
    missing_events = await service.list_events("ws_missing")

    assert created.repo_url == "git@github.com:example/v1.git"
    assert created.env_profile == "aira"
    assert created.requires_database is True
    assert fetched is not None
    assert fetched.requires_database is True
    assert events is not None
    assert [event.event_type for event in events] == ["workspace.created"]
    assert missing_events is None


@pytest.mark.unit
async def test_get_runtime_returns_snapshot_and_none_for_missing_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    class FakeRuntimeInspector:
        async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
            assert compose_project_name == "awf_ws_service_runtime"
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
    profile = WorkspaceProfile.model_validate(
        {
            "name": "runtime-endpoints",
            "services": [{"name": "app", "image": "example/app:latest"}],
            "app_endpoints": [
                {
                    "name": "app",
                    "service": "app",
                    "port": 3000,
                    "path": "/",
                    "health": {"path": "/healthz"},
                    "visibility": "agent",
                }
            ],
        }
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Observe runtime",
            task_prompt="Inspect runtime.",
            agent="codex",
            test_commands=[],
            resolved_profile=profile.model_dump(mode="json", by_alias=True),
        )
        workspace.compose_project_name = "awf_ws_service_runtime"
        await session.commit()

    snapshot = await service.get_runtime(workspace.id)
    missing = await service.get_runtime("ws_missing")

    assert snapshot is not None
    assert snapshot.workspace_id == workspace.id
    assert snapshot.compose_project_name == "awf_ws_service_runtime"
    assert snapshot.stack_state == "running"
    assert snapshot.logs_available is True
    assert snapshot.control_available is True
    assert snapshot.reason is None
    assert len(snapshot.services) == 1
    assert snapshot.services[0].name == "agent"
    assert snapshot.services[0].health == "healthy"
    assert [endpoint.model_dump(mode="json") for endpoint in snapshot.app_endpoints] == [
        {
            "name": "app",
            "service": "app",
            "scheme": "http",
            "port": 3000,
            "path": "/",
            "internal_url": "http://app:3000/",
            "visibility": "agent",
            "health": {
                "path": "/healthz",
                "method": "GET",
                "expected_status": 200,
                "internal_url": "http://app:3000/healthz",
            },
        }
    ]
    assert missing is None


@pytest.mark.unit
async def test_list_operations_respects_limit_and_none_for_missing_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
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

    rows = await service.list_operations(workspace.id, limit=2)
    missing = await service.list_operations("ws_missing")

    assert rows is not None
    assert [row.id for row in rows] == [stop.id, validate.id]
    assert [row.type for row in rows] == ["stop", "validate"]
    assert [row.status for row in rows] == ["pending", "running"]
    assert missing is None


@pytest.mark.unit
async def test_global_operation_helpers_filter_and_get(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    base = datetime(2026, 4, 25, 13, 0, tzinfo=UTC)
    async with factory() as session:
        ws_repo = WorkspaceRepository(session)
        first_workspace = await ws_repo.create(
            repo_url="git@github.com:example/first.git",
            branch_base="main",
            task_title="First operations",
            task_prompt="List first operations.",
            agent="codex",
            test_commands=[],
        )
        second_workspace = await ws_repo.create(
            repo_url="git@github.com:example/second.git",
            branch_base="main",
            task_title="Second operations",
            task_prompt="List second operations.",
            agent="codex",
            test_commands=[],
        )
        repo = OperationRepository(session)
        first_operation = await repo.create(
            workspace_id=first_workspace.id,
            operation_type=OperationType.create,
            status=OperationStatus.succeeded,
        )
        second_operation = await repo.create(
            workspace_id=second_workspace.id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
        )
        first_operation.created_at = base
        second_operation.created_at = base + timedelta(seconds=1)
        await session.commit()

    rows = await service.list_all_operations(status=OperationStatus.running)
    operation = await service.get_operation(first_operation.id)
    missing = await service.get_operation("op_missing")

    assert [row.id for row in rows] == [second_operation.id]
    assert rows[0].workspace_id == second_workspace.id
    assert operation is not None
    assert operation.id == first_operation.id
    assert operation.type == "create"
    assert missing is None


@pytest.mark.unit
async def test_operation_page_helpers_return_keyset_cursor_pages(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    base = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/paged.git",
            branch_base="main",
            task_title="Page operations",
            task_prompt="List paged operations.",
            agent="codex",
            test_commands=[],
        )
        repo = OperationRepository(session)
        older = await repo.create(
            workspace_id=workspace.id,
            operation_type=OperationType.create,
            status=OperationStatus.succeeded,
        )
        newer = await repo.create(
            workspace_id=workspace.id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
        )
        older.created_at = base
        newer.created_at = base + timedelta(seconds=1)
        await session.commit()

    first_page = await service.list_all_operations_page(limit=2)
    cursor = build_operation_list_response(first_page.rows, limit=1).next_cursor
    assert cursor is not None
    page = await service.list_all_operations_page(limit=2, cursor=cursor)
    workspace_page = await service.list_operations_page(workspace.id, limit=2, cursor=cursor)
    missing_workspace_page = await service.list_operations_page(
        "ws_missing",
        cursor="not-a-cursor",
    )

    assert [row.id for row in page.rows] == [older.id]
    assert workspace_page is not None
    assert [row.id for row in workspace_page.rows] == [older.id]
    assert missing_workspace_page is None


@pytest.mark.unit
async def test_workspace_service_control_wrappers_commit_results(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    class RecordingStopper:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        async def __call__(self, compose_project_name: str | None) -> None:
            self.calls.append(compose_project_name)

    class RecordingCleaner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def cleanup(
            self,
            *,
            workspace_id: str,
            repo_url: str,
            companion_worktrees: tuple[tuple[str, str], ...] = (),
            compose_project_name: str | None = None,
            compose_file_path: Path | None = None,
            worktree_host_path: Path | None = None,
            remove_volumes: bool = True,
            remove_worktree: bool = True,
        ) -> list[str]:
            _ = companion_worktrees
            assert repo_url == "git@github.com:example/controls.git"
            assert compose_file_path is None
            assert worktree_host_path is None
            assert remove_volumes is False
            assert remove_worktree is True
            self.calls.append(workspace_id)
            return []

    stopper = RecordingStopper()
    cleaner = RecordingCleaner()
    service = WorkspaceService(
        factory,
        project_stopper=stopper,
        cleaner_factory=lambda: cleaner,
    )
    async with factory() as session:
        repo = WorkspaceRepository(session)
        cancelled = await repo.create(
            repo_url="git@github.com:example/controls.git",
            branch_base="main",
            task_title="Cancel wrapper",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        stopped = await repo.create(
            repo_url="git@github.com:example/controls.git",
            branch_base="main",
            task_title="Stop wrapper",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        destroyed = await repo.create(
            repo_url="git@github.com:example/controls.git",
            branch_base="main",
            task_title="Destroy wrapper",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        cancelled.status = WorkspaceStatus.ready.value
        stopped.status = WorkspaceStatus.running.value
        destroyed.status = WorkspaceStatus.failed.value
        cancelled.compose_project_name = "awf_cancel"
        stopped.compose_project_name = "awf_stop"
        destroyed.compose_project_name = "awf_destroy"
        await session.commit()
        cancelled_id = cancelled.id
        stopped_id = stopped.id
        destroyed_id = destroyed.id

    cancel_response = await service.cancel_workspace(
        cancelled_id,
        reason="operator cancel",
        stop_stack=False,
    )
    stop_response = await service.stop_workspace(stopped_id, reason="operator stop")
    destroy_response = await service.destroy_workspace(
        destroyed_id,
        remove_volumes=False,
        remove_worktree=True,
    )

    assert cancel_response.status == WorkspaceStatus.cancelled
    assert stop_response.status == WorkspaceStatus.cancelled
    assert destroy_response.status == WorkspaceStatus.destroyed
    assert stopper.calls == ["awf_stop"]
    assert cleaner.calls == [destroyed_id]


@pytest.mark.unit
async def test_retry_workspace_errors_and_missing_source_attempt_fallback(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        active = await repo.create(
            repo_url="git@github.com:example/retry.git",
            branch_base="main",
            task_title="Active retry",
            task_prompt="p",
            agent=AgentRuntime.codex.value,
            test_commands=[],
        )
        failed = await repo.create(
            repo_url="git@github.com:example/retry.git",
            branch_base="main",
            task_title="Failed retry",
            task_prompt="p",
            task_external_id="RETRY-FALLBACK",
            task_class="test_task",
            owned_paths=["src/**"],
            agent=AgentRuntime.codex.value,
            test_commands=["pytest -q"],
        )
        failed.status = WorkspaceStatus.failed.value
        await session.commit()
        active_id = active.id
        failed_id = failed.id

    async with factory() as session:
        with pytest.raises(WorkspaceRetryNotFoundError):
            await retry_workspace_row(session, "ws_missing")
        with pytest.raises(WorkspaceRetryNotAllowedError):
            await retry_workspace_row(session, active_id)
        result = await retry_workspace_row(
            session,
            failed_id,
            provider_readiness_override=True,
            provider_readiness_override_reason="observability test fixture",
        )
        await session.commit()

    async with factory() as session:
        retry_attempt = await TaskAttemptRepository(session).get_by_workspace_id(
            result.new_workspace.id
        )
        assert retry_attempt is not None
        retry_task = await TaskRepository(session).get(retry_attempt.task_id)

    assert result.source_workspace_id == failed_id
    assert result.attempt_number == 1
    assert retry_task is not None
    assert retry_task.idempotency_key == f"retry-source-workspace:{failed_id}"


@pytest.mark.unit
async def test_read_log_rejects_missing_and_out_of_root_streams_then_reads_chunk(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    valid_log = log_root / "workspace" / "agent.log"
    valid_log.parent.mkdir()
    valid_log.write_text("hello\nworld\n", encoding="utf-8")
    missing_file = log_root / "workspace" / "missing.log"
    outside_log = tmp_path / "outside.log"
    outside_log.write_text("outside", encoding="utf-8")
    service = WorkspaceService(factory, log_root=log_root)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/logs.git",
            branch_base="main",
            task_title="Read logs",
            task_prompt="Read workspace logs.",
            agent="codex",
            test_commands=[],
        )
        stream_repo = WorkspaceLogStreamRepository(session)
        await stream_repo.create_or_get(
            workspace_id=workspace.id,
            stream_id="agent.stdout",
            source="agent",
            name="stdout",
            kind="stdout",
            path=str(valid_log),
        )
        await stream_repo.create_or_get(
            workspace_id=workspace.id,
            stream_id="agent.missing",
            source="agent",
            name="missing",
            kind="stdout",
            path=str(missing_file),
        )
        await stream_repo.create_or_get(
            workspace_id=workspace.id,
            stream_id="agent.outside",
            source="agent",
            name="outside",
            kind="stdout",
            path=str(outside_log),
        )
        await session.commit()
        workspace_id = workspace.id

    missing_workspace = await service.read_log("ws_missing", "agent.stdout")
    missing_stream = await service.read_log(workspace_id, "missing.stream")
    missing_file_result = await service.read_log(workspace_id, "agent.missing")
    outside_result = await service.read_log(workspace_id, "agent.outside")
    chunk = await service.read_log(workspace_id, "agent.stdout", offset=6, limit_bytes=5)
    streams = await service.list_logs(workspace_id)
    missing_logs = await service.list_logs("ws_missing")

    assert missing_workspace is None
    assert missing_stream is None
    assert missing_file_result is None
    assert outside_result is None
    assert chunk == {
        "stream_id": "agent.stdout",
        "offset": 6,
        "next_offset": 11,
        "eof": False,
        "text": "world",
    }
    assert streams is not None
    assert [stream.stream_id for stream in streams] == [
        "agent.stdout",
        "agent.missing",
        "agent.outside",
    ]
    assert missing_logs is None


@pytest.mark.unit
def test_owned_path_overlap_warning_parsing_ignores_malformed_payload_items() -> None:
    workspace = Workspace(
        events=[
            WorkspaceEvent(event_type="other", payload=None),
            WorkspaceEvent(
                event_type="workspace.owned_path_overlap_risk",
                payload=None,
            ),
            WorkspaceEvent(
                event_type="workspace.owned_path_overlap_risk",
                payload={
                    "warning_code": "OWNED_PATH_OVERLAP_RISK",
                    "message": "overlap",
                    "workspace_ids": ["ws_a", 42, "ws_b"],
                    "overlaps": [
                        {"workspace_id": "ws_a", "existing_path": "src/**"},
                        {
                            "workspace_id": "ws_b",
                            "existing_path": "src/awf/**",
                            "requested_path": "src/awf/service/workspaces.py",
                        },
                        "bad",
                    ],
                },
            ),
        ]
    )

    warnings = owned_path_overlap_warnings(workspace)

    assert len(warnings) == 1
    assert warnings[0].workspace_ids == ["ws_a", "ws_b"]
    assert len(warnings[0].overlaps) == 1
    assert warnings[0].overlaps[0].workspace_id == "ws_b"


@pytest.mark.unit
def test_owned_path_overlap_warning_parsing_treats_none_events_as_empty() -> None:
    workspace = SimpleNamespace(events=None)

    assert owned_path_overlap_warnings(workspace) == []  # type: ignore[arg-type]


@pytest.mark.unit
def test_owned_path_warning_payloads_dedupe_ids_and_tolerate_non_lists() -> None:
    payload = owned_path_overlap_warning_payload(
        [
            OwnedPathOverlap(
                workspace_id="ws_same",
                existing_path="src/**",
                requested_path="src/app.py",
            ),
            OwnedPathOverlap(
                workspace_id="ws_same",
                existing_path="tests/**",
                requested_path="tests/test_app.py",
            ),
        ]
    )
    workspace = Workspace(
        events=[
            WorkspaceEvent(
                event_type="workspace.owned_path_overlap_risk",
                payload={
                    "workspace_ids": "ws_not_a_list",
                    "overlaps": "not a list",
                },
            )
        ]
    )

    warnings = owned_path_overlap_warnings(workspace)

    assert payload["workspace_ids"] == ["ws_same"]
    assert len(payload["overlaps"]) == 2
    assert warnings[0].workspace_ids == []
    assert warnings[0].overlaps == []


@pytest.mark.unit
def test_workspace_retry_error_allows_custom_message() -> None:
    error = WorkspaceRetryError("custom retry failure", detail={"workspace_id": "ws_1"})

    assert str(error) == "custom retry failure"
    assert error.message == "custom retry failure"
    assert error.detail == {"workspace_id": "ws_1"}


@pytest.mark.unit
def test_workspace_retry_error_uses_default_message() -> None:
    error = WorkspaceRetryError()

    assert str(error) == "Workspace retry failed."
    assert error.message == "Workspace retry failed."
    assert error.detail is None


@pytest.mark.unit
def test_task_policy_snapshot_persists_empty_companion_list() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/policy.git", "base_branch": "main"},
        task={"title": "Policy snapshot", "prompt": "p", "agent": "codex"},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "observability test fixture",
        },
    )

    policy = workspace_create_task_policy_snapshot(request)

    assert "companions" in policy
    assert policy["companions"] == []


@pytest.mark.unit
def test_v2_task_policy_and_profile_tier_helpers_cover_noop_and_updates() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/policy.git", "base_branch": "main"},
        task={
            "title": "Policy snapshot",
            "prompt": "p",
            "agent": "codex",
            "model": "gpt-5.3-codex",
            "out_of_scope_changes": {
                "mode": "block",
                "allowlist_patterns": ["generated/**"],
            },
        },
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "observability test fixture",
        },
    )
    profile = WorkspaceProfile(name="unit-profile")

    policy = workspace_create_task_policy_snapshot(request)
    unchanged = profile_with_requested_tier(profile, 1)
    changed = profile_with_requested_tier(profile, 3)

    assert policy == {
        "agent_model": "gpt-5.3-codex",
        "companions": [],
        "out_of_scope_changes": {
            "mode": "block",
            "allowlist_patterns": ["generated/**"],
        },
        "resource_reservation_request": {},
        "validation": {"requested_tier": 1},
    }
    assert unchanged is profile
    assert changed.validation.requested_tier == 3
    assert profile.validation.requested_tier == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source_branch", "expected_source"),
    [("release/cut", "release/cut"), (None, "development")],
)
def test_sync_release_pr_snapshot_records_release_sync_block(
    source_branch: str | None,
    expected_source: str,
) -> None:
    repo: dict[str, object] = {"url": "git@github.com:example/rel.git", "base_branch": "master"}
    if source_branch is not None:
        repo["source_branch"] = source_branch
    request = WorkspaceCreateRequest(
        repo=repo,
        task={"title": "Release sync", "prompt": "p", "agent": "codex", "kind": "sync_release_pr"},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "release sync fixture",
        },
    )

    policy = workspace_create_task_policy_snapshot(request)

    assert policy["release_sync"] == {
        "source_branch": expected_source,
        "target_branch": "master",
    }


@pytest.mark.unit
def test_feature_branch_pr_snapshot_omits_release_sync_block() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/feat.git", "base_branch": "main"},
        task={"title": "Feature", "prompt": "p", "agent": "codex", "kind": "feature_branch_pr"},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "feature fixture",
        },
    )

    policy = workspace_create_task_policy_snapshot(request)

    assert "release_sync" not in policy


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "requested_auto_merge", "expected"),
    [
        ("sync_release_pr", True, False),
        ("feature_branch_pr", True, True),
        ("feature_branch_pr", False, False),
    ],
)
def test_effective_auto_merge_forces_false_for_release_sync(
    kind: str,
    requested_auto_merge: bool,
    expected: bool,
) -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/am.git", "base_branch": "main"},
        task={
            "title": "Auto merge",
            "prompt": "p",
            "agent": "codex",
            "kind": kind,
            "auto_merge": requested_auto_merge,
        },
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "auto merge fixture",
        },
    )

    assert _effective_auto_merge(request) is expected


@pytest.mark.unit
def test_assert_supported_direct_create_task_kind_guards_unsupported() -> None:
    _assert_supported_direct_create_task_kind("feature_branch_pr")
    _assert_supported_direct_create_task_kind("sync_release_pr")
    with pytest.raises(ValueError, match="unsupported task kind"):
        _assert_supported_direct_create_task_kind("sync_feature_pr")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("512mb", 0.5),
        ("2g", 2.0),
        ("3", 3.0),
        ("not-memory", None),
        ("12xb", None),
        ("abcmb", None),
    ],
)
def test_parse_memory_gb_handles_blank_units_and_invalid_values(
    raw: str | None,
    expected: float | None,
) -> None:
    assert _parse_memory_gb(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent", "model"),
    [
        (AgentRuntime.codex, "gpt-5.5"),
        (AgentRuntime.gemini, "gemini-3.1-pro-preview"),
        (AgentRuntime.claude_code, "claude-opus-4-7"),
        (AgentRuntime.opencode, "ollama/kimi-k2.6:cloud"),
    ],
)
def test_effective_agent_identity_uses_central_defaults(
    agent: AgentRuntime,
    model: str,
) -> None:
    identity = effective_agent_identity(agent=agent, task_policy={})

    assert identity.model == model
    assert identity.effort == "xhigh"
    assert identity.model_source == "default"
    assert identity.effort_source == "default"


@pytest.mark.unit
@pytest.mark.parametrize(
    "task_policy",
    [
        {"agent_model": ""},
        {"agent_model": "   "},
        {"agent_model": 123},
        {"agent_model": None},
    ],
)
def test_effective_agent_identity_ignores_blank_or_malformed_model_policy(
    task_policy: dict[str, object],
) -> None:
    identity = effective_agent_identity(
        agent=AgentRuntime.codex,
        task_policy=task_policy,
    )

    assert identity.model == "gpt-5.5"
    assert identity.model_source == "default"
    assert identity.effort == "xhigh"


@pytest.mark.unit
def test_effective_agent_identity_prefers_explicit_requested_model() -> None:
    identity = effective_agent_identity(
        agent=AgentRuntime.codex,
        task_policy={"agent_model": "gpt-custom"},
    )

    assert identity.model == "gpt-custom"
    assert identity.model_source == "task_policy"
    assert identity.effort == "xhigh"
    assert identity.effort_source == "default"


@pytest.mark.unit
def test_effective_agent_identity_prefers_explicit_effort_policy() -> None:
    identity = effective_agent_identity(
        agent=AgentRuntime.claude_code,
        task_policy={"agent_effort": "max"},
    )

    assert identity.model == "claude-opus-4-7"
    assert identity.model_source == "default"
    assert identity.effort == "max"
    assert identity.effort_source == "task_policy"


@pytest.mark.unit
def test_effective_agent_identity_returns_unavailable_for_unknown_agent() -> None:
    identity = effective_agent_identity(agent="future_agent", task_policy=None)

    assert identity.model is None
    assert identity.model_source == "unavailable"
    assert identity.effort is None
    assert identity.effort_source == "unavailable"


@pytest.mark.unit
def test_recovery_summary_is_none_without_reverse_transition() -> None:
    base = datetime(2026, 4, 27, 20, 0, tzinfo=UTC)
    workspace = _workspace_for_recovery(
        status=WorkspaceStatus.monitoring_pr,
        created_at=base,
        events=[
            _recovery_event(
                event_id="evt_created",
                event_type="workspace.created",
                occurred_at=base,
                new_state=WorkspaceStatus.requested.value,
                reason_code="CREATED",
            ),
            _recovery_event(
                event_id="evt_forward",
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.validating.value,
                new_state=WorkspaceStatus.monitoring_pr.value,
                reason_code="PR_OPENED",
            ),
        ],
    )

    assert workspace_recovery_summary(workspace) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_recovery_summary_pairs_reverse_transition_with_monitor_event_payload() -> None:
    base = datetime(2026, 4, 27, 20, 30, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=40)
    dispatch_payload = {
        "reason": "STALE_OVERLAP",
        "req_action": "rebase",
        "recovery_mode": "rebase_only",
        "pr_number": 42,
    }
    workspace = _workspace_for_recovery(
        created_at=base,
        events=[
            _recovery_event(
                event_id="evt_forward",
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=20),
                old_state=WorkspaceStatus.validating.value,
                new_state=WorkspaceStatus.monitoring_pr.value,
                reason_code="PR_OPENED",
            ),
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
            ),
            _recovery_event(
                event_id="evt_dispatch",
                event_type="monitor.recovery_dispatched",
                occurred_at=reverse_at + timedelta(seconds=1),
                old_state=WorkspaceStatus.ready.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
                payload=dispatch_payload,
            ),
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.from_state == "monitoring_pr"
    assert summary.to_state == "ready"
    assert summary.reason_code == "STALE_OVERLAP"
    assert summary.action == "rebase"
    assert summary.recovery_mode == "rebase_only"
    assert summary.started_at == reverse_at
    assert summary.payload == dispatch_payload
    assert "monitoring_pr -> ready" in summary.summary
    assert "STALE_OVERLAP" in summary.summary


@pytest.mark.unit
def test_recovery_summary_surfaces_active_pr_monitor_operation() -> None:
    base = datetime(2026, 4, 27, 21, 0, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=60)
    operation_payload = {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "reason": "Coverage validation must be refreshed.",
        "reason_code": "COVERAGE_STALE",
        "requested_action": "validate",
        "recovery_mode": "validate_only",
    }
    operation = _recovery_operation(
        operation_id="op_validate_recovery",
        status=OperationStatus.running.value,
        created_at=reverse_at - timedelta(seconds=2),
        started_at=reverse_at + timedelta(seconds=3),
        payload=operation_payload,
    )
    workspace = _workspace_for_recovery(
        status=WorkspaceStatus.validating,
        created_at=base,
        operations=[operation],
        events=[
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
            )
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "COVERAGE_STALE"
    assert summary.action == "validate"
    assert summary.recovery_mode == "validate_only"
    assert summary.current_operation is not None
    assert summary.current_operation.id == "op_validate_recovery"
    assert summary.current_operation.type == "validate"
    assert summary.current_operation.status == "running"
    assert summary.current_operation.payload == operation_payload
    assert "validate recovery is running" in summary.summary
    assert "workspace is validating" in summary.summary


@pytest.mark.unit
def test_recovery_summary_uses_latest_reverse_recovery_pair() -> None:
    base = datetime(2026, 4, 27, 21, 30, tzinfo=UTC)
    older_reverse = base + timedelta(seconds=30)
    latest_reverse = base + timedelta(seconds=90)
    workspace = _workspace_for_recovery(
        created_at=base,
        events=[
            _recovery_event(
                event_id="evt_old_reverse",
                event_type="workspace.state_changed",
                occurred_at=older_reverse,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
            ),
            _recovery_event(
                event_id="evt_old_dispatch",
                event_type="monitor.recovery_dispatched",
                occurred_at=older_reverse + timedelta(seconds=1),
                reason_code="RECOVERY_DISPATCH",
                payload={
                    "reason": "STALE_TARGET_ADVANCED",
                    "req_action": "rebase",
                    "recovery_mode": "rebase_only",
                },
            ),
            _recovery_event(
                event_id="evt_latest_reverse",
                event_type="workspace.state_changed",
                occurred_at=latest_reverse,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
            ),
            _recovery_event(
                event_id="evt_latest_dispatch",
                event_type="monitor.recovery_dispatched",
                occurred_at=latest_reverse + timedelta(seconds=1),
                reason_code="RECOVERY_DISPATCH",
                payload={
                    "reason": "STALE_OVERLAP",
                    "req_action": "validate",
                    "recovery_mode": "validate_only",
                },
            ),
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.started_at == latest_reverse
    assert summary.reason_code == "STALE_OVERLAP"
    assert summary.action == "validate"
    assert summary.recovery_mode == "validate_only"
    assert "STALE_TARGET_ADVANCED" not in summary.summary


@pytest.mark.unit
def test_recovery_summary_uses_inactive_operator_recovery_operation() -> None:
    base = datetime(2026, 4, 27, 21, 40, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=30)
    workspace = _workspace_for_recovery(
        created_at=base,
        operations=[
            _recovery_operation(
                operation_id="op_finished_monitor",
                operation_type=OperationType.validate.value,
                status=OperationStatus.succeeded.value,
                created_at=reverse_at + timedelta(seconds=1),
                payload={"owner": "pr_monitor"},
            ),
            _recovery_operation(
                operation_id="op_wrong_type",
                operation_type=OperationType.stop.value,
                status=OperationStatus.pending.value,
                created_at=reverse_at + timedelta(seconds=2),
                payload={"source": "operator_api", "recovery_mode": "rebase_only"},
            ),
            _recovery_operation(
                operation_id="op_missing_payload",
                operation_type=OperationType.validate.value,
                status=OperationStatus.pending.value,
                created_at=reverse_at + timedelta(seconds=3),
                payload=None,
            ),
            _recovery_operation(
                operation_id="op_operator_recovery",
                operation_type=OperationType.retry.value,
                status=OperationStatus.succeeded.value,
                created_at=reverse_at + timedelta(seconds=4),
                payload={"source": "operator_api", "recovery_mode": "validate_only"},
            ),
        ],
        events=[
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="STALE_TARGET_ADVANCED",
            )
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "STALE_TARGET_ADVANCED"
    assert summary.action is None
    assert summary.recovery_mode == "validate_only"
    assert summary.current_operation is None
    assert summary.payload == {
        "source": "operator_api",
        "recovery_mode": "validate_only",
    }
    assert "validate-only recovery" in summary.summary
