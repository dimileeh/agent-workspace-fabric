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
from awf.db.repositories import (
    OperationRepository,
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
    workspace_pricing_metadata,
    workspace_usage_summary,
)
from awf.service.workspaces import (
    WorkspaceRetryNotAllowedError,
    WorkspaceRetryNotFoundError,
    WorkspaceService,
    retry_workspace_row,
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
            self.call_details: list[dict[str, object]] = []

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
            self.calls.append(workspace_id)
            self.call_details.append(
                {
                    "workspace_id": workspace_id,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                }
            )
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
    # cancel(stop_stack=False) leaves teardown to cleanup, but stop now runs a
    # full compose down (preserving the worktree), and destroy removes it.
    assert stopper.calls == []
    assert cleaner.calls == [stopped_id, destroyed_id]
    assert cleaner.call_details == [
        {"workspace_id": stopped_id, "remove_volumes": True, "remove_worktree": False},
        {"workspace_id": destroyed_id, "remove_volumes": False, "remove_worktree": True},
    ]


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
