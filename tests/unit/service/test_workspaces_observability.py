"""Workspace service observability helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest, WorkspaceCreateV2Request
from awf.db.base import Base
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
from awf.db.session import make_engine, make_session_factory
from awf.profiles.models import WorkspaceProfile
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.workspace_observability import (
    _latest_reverse_state_event,
    effective_agent_identity,
    workspace_identity_usage_payload,
    workspace_lifecycle_summary,
    workspace_observability_payload,
    workspace_recovery_summary,
    workspace_usage_summary,
)
from awf.service.workspaces import (
    WorkspaceRetryError,
    WorkspaceRetryNotAllowedError,
    WorkspaceRetryNotFoundError,
    WorkspaceService,
    _parse_memory_gb,
    owned_path_overlap_warning_payload,
    owned_path_overlap_warnings,
    profile_with_requested_tier,
    retry_workspace_row,
    v2_task_policy_snapshot,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.mark.unit
async def test_workspace_service_round_trips_policy_metadata(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    request = WorkspaceCreateV2Request(
        repo={"url": "git@github.com:example/service.git", "base_branch": "main"},
        task={
            "title": "Update dependency",
            "prompt": "Bump the dependency and adjust tests.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "task_class": "dependency_task",
            "owned_paths": ["pyproject.toml", "uv.lock"],
        },
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["uv run pytest -q"], "requested_tier": 1},
        resources={},
    )

    created = await service.create_v2(request)
    fetched = await service.get(created.id)
    listed = await service.list(limit=10)

    assert created.task_class == "dependency_task"
    assert created.owned_paths == ["pyproject.toml", "uv.lock"]
    assert fetched is not None
    assert fetched.task_class == "dependency_task"
    assert fetched.owned_paths == ["pyproject.toml", "uv.lock"]
    assert listed[0].task_class == "dependency_task"
    assert listed[0].owned_paths == ["pyproject.toml", "uv.lock"]


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
        )
    )

    events = await service.list_events(created.id, event_type="workspace.created")
    missing_events = await service.list_events("ws_missing")

    assert created.repo_url == "git@github.com:example/v1.git"
    assert created.env_profile == "python"
    assert created.requires_database is True
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
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Observe runtime",
            task_prompt="Inspect runtime.",
            agent="codex",
            test_commands=[],
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
            compose_project_name: str | None = None,
            compose_file_path: Path | None = None,
            worktree_host_path: Path | None = None,
            remove_volumes: bool = True,
            remove_worktree: bool = True,
        ) -> list[str]:
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
async def test_retry_workspace_errors_and_missing_source_task_fallback(
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
        task = await TaskRepository(session).create_or_get(
            repo_url=failed.repo_url,
            base_branch=failed.branch_base,
            title=failed.task_title,
            prompt=failed.task_prompt,
            external_id=failed.task_external_id,
            idempotency_key=None,
            task_class=failed.task_class,
            owned_paths=list(failed.owned_paths),
        )
        source_attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=failed,
        )
        source_attempt.task_id = "task_missing_from_retry_source"
        await session.commit()
        active_id = active.id
        failed_id = failed.id

    async with factory() as session:
        with pytest.raises(WorkspaceRetryNotFoundError):
            await retry_workspace_row(session, "ws_missing")
        with pytest.raises(WorkspaceRetryNotAllowedError):
            await retry_workspace_row(session, active_id)
        result = await retry_workspace_row(session, failed_id)
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
def test_v2_task_policy_and_profile_tier_helpers_cover_noop_and_updates() -> None:
    request = WorkspaceCreateV2Request(
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
    )
    profile = WorkspaceProfile(name="unit-profile")

    policy = v2_task_policy_snapshot(request)
    unchanged = profile_with_requested_tier(profile, 1)
    changed = profile_with_requested_tier(profile, 3)

    assert policy == {
        "agent_model": "gpt-5.3-codex",
        "out_of_scope_changes": {
            "mode": "block",
            "allowlist_patterns": ["generated/**"],
        },
    }
    assert unchanged is profile
    assert changed.validation.requested_tier == 3
    assert profile.validation.requested_tier == 1


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
        (AgentRuntime.gemini, "gemini-3-pro-preview"),
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


@pytest.mark.unit
def test_recovery_summary_bounds_json_payload_from_previous_recovery_event() -> None:
    base = datetime(2026, 4, 27, 21, 42, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=20)
    payload = {
        "reason_code": "PAYLOAD_RECOVERY",
        "action": "retry",
        "when": reverse_at,
        "nested": {f"k{index}": index for index in range(33)},
        "items": list(range(25)),
        "deep": {"a": {"b": {"c": {"d": {"too": "deep"}}}}},
        "path": Path("artifact.txt"),
        **{f"extra_{index}": index for index in range(40)},
    }
    workspace = _workspace_for_recovery(
        created_at=base,
        events=[
            _recovery_event(
                event_id="evt_previous_dispatch",
                event_type="monitor.recovery_dispatched",
                occurred_at=base + timedelta(seconds=5),
                reason_code="RECOVERY_DISPATCH",
                payload=payload,
            ),
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="RECOVERY_DISPATCH",
            ),
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "PAYLOAD_RECOVERY"
    assert summary.action == "retry"
    assert summary.recovery_mode is None
    assert "AWF dispatched retry." in summary.summary
    assert summary.payload is not None
    assert summary.payload["when"] == reverse_at.isoformat()
    assert summary.payload["nested"]["__truncated__"] is True
    assert summary.payload["items"][-1] == "__truncated__"
    assert summary.payload["deep"]["a"]["b"]["c"]["d"].startswith("{'too':")
    assert summary.payload["path"] == "artifact.txt"
    assert summary.payload["__truncated__"] is True


@pytest.mark.unit
def test_recovery_summary_handles_payloadless_workspace_without_status() -> None:
    base = datetime(2026, 4, 27, 21, 44, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=10)
    workspace = SimpleNamespace(
        id="ws_recovery",
        status=None,
        created_at=base,
        operations=[],
        events=[
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="STALE_OVERLAP",
            )
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "STALE_OVERLAP"
    assert summary.action is None
    assert summary.recovery_mode is None
    assert summary.payload is None
    assert summary.summary == "Reverted monitoring_pr -> ready for STALE_OVERLAP."


@pytest.mark.unit
def test_recovery_summary_uses_reverse_reason_when_payloads_are_empty() -> None:
    base = datetime(2026, 4, 27, 22, 0, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=45)
    workspace = SimpleNamespace(
        id="ws_recovery",
        status="",
        created_at=base,
        operations=[],
        events=[
            _recovery_event(
                event_id="evt_reverse",
                event_type="workspace.state_changed",
                occurred_at=reverse_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.ready.value,
                reason_code="MANUAL_RECOVERY",
            )
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "MANUAL_RECOVERY"
    assert summary.action is None
    assert summary.recovery_mode is None
    assert summary.payload is None
    assert summary.summary == "Reverted monitoring_pr -> ready for MANUAL_RECOVERY."


@pytest.mark.unit
def test_recovery_summary_filters_non_recovery_operations_before_latest_match() -> None:
    base = datetime(2026, 4, 27, 22, 15, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=30)
    valid_payload = {
        "source": "operator_api",
        "recovery_mode": "validate_only",
        "requested_action": "validate",
        "reason_code": "OPERATOR_REFRESH",
    }
    workspace = _workspace_for_recovery(
        status=WorkspaceStatus.validating,
        created_at=base,
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
        operations=[
            _recovery_operation(
                operation_id="op_finished",
                status=OperationStatus.succeeded.value,
                created_at=base,
                payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
            ),
            _recovery_operation(
                operation_id="op_cleanup",
                operation_type="cleanup",
                status=OperationStatus.pending.value,
                created_at=base + timedelta(seconds=1),
                payload={"source": "pr_monitor", "recovery_mode": "validate_only"},
            ),
            _recovery_operation(
                operation_id="op_bad_payload",
                status=OperationStatus.pending.value,
                created_at=base + timedelta(seconds=2),
                payload=None,
            ),
            _recovery_operation(
                operation_id="op_manual_mode",
                status=OperationStatus.pending.value,
                created_at=base + timedelta(seconds=3),
                payload={"source": "operator_api", "recovery_mode": "manual"},
            ),
            _recovery_operation(
                operation_id="op_operator_validate",
                status=OperationStatus.pending.value,
                created_at=base + timedelta(seconds=4),
                payload=valid_payload,
            ),
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "OPERATOR_REFRESH"
    assert summary.action == "validate"
    assert summary.recovery_mode == "validate_only"
    assert summary.current_operation is not None
    assert summary.current_operation.id == "op_operator_validate"
    assert summary.current_operation.payload == valid_payload


@pytest.mark.unit
def test_recovery_summary_bounds_json_safe_payload_values() -> None:
    base = datetime(2026, 4, 27, 22, 45, tzinfo=UTC)
    reverse_at = base + timedelta(seconds=20)

    class OpaquePayloadValue:
        def __str__(self) -> str:
            return "opaque-payload-value"

    event_payload: dict[str, object] = {
        "reason": "MANUAL_RUNTIME_RECOVERY",
        "action": "retry",
        "recovery_mode": "manual",
        "at": base,
        "nested": {f"nested_{index}": index for index in range(35)},
        "items": list(range(21)),
        "deep": {"level1": {"level2": {"level3": {"level4": {"value": "hidden"}}}}},
        "opaque": OpaquePayloadValue(),
    }
    event_payload.update({f"extra_{index}": index for index in range(40)})
    workspace = _workspace_for_recovery(
        created_at=base,
        events=[
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
                reason_code="RECOVERY_DISPATCH",
                payload=event_payload,
            ),
        ],
    )

    summary = workspace_recovery_summary(workspace)  # type: ignore[arg-type]

    assert summary is not None
    assert summary.reason_code == "MANUAL_RUNTIME_RECOVERY"
    assert summary.action == "retry"
    assert summary.recovery_mode == "manual"
    assert "AWF dispatched retry." in summary.summary
    assert summary.payload is not None
    assert summary.payload["at"] == base.isoformat()
    assert summary.payload["nested"]["__truncated__"] is True
    assert summary.payload["items"][-1] == "__truncated__"
    assert summary.payload["deep"]["level1"]["level2"]["level3"]["level4"] == (
        "{'value': 'hidden'}"
    )
    assert summary.payload["opaque"] == "opaque-payload-value"
    assert summary.payload["__truncated__"] is True


@pytest.mark.unit
def test_latest_reverse_state_event_scans_from_most_recent_event() -> None:
    base = datetime(2026, 4, 27, 21, 45, tzinfo=UTC)

    class EarlierStateChange:
        event_type = "workspace.state_changed"

        @property
        def old_state(self) -> str:
            raise AssertionError("older events should not be inspected")

    latest_reverse = _recovery_event(
        event_id="evt_latest_reverse",
        event_type="workspace.state_changed",
        occurred_at=base + timedelta(seconds=60),
        old_state=WorkspaceStatus.monitoring_pr.value,
        new_state=WorkspaceStatus.ready.value,
    )

    assert _latest_reverse_state_event([EarlierStateChange(), latest_reverse]) is latest_reverse


@pytest.mark.unit
def test_lifecycle_summary_closes_reached_stages_and_tracks_active_duration() -> None:
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.running,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.created",
                occurred_at=base,
                new_state=WorkspaceStatus.requested.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=25),
                old_state=WorkspaceStatus.provisioning.value,
                new_state=WorkspaceStatus.ready.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=40),
                old_state=WorkspaceStatus.ready.value,
                new_state=WorkspaceStatus.running.value,
            ),
        ],
    )

    summary = workspace_lifecycle_summary(
        workspace,
        now=base + timedelta(seconds=70),
    )
    stages = {item.stage: item for item in summary}

    assert stages["requested"].started_at == base
    assert stages["requested"].ended_at == base + timedelta(seconds=10)
    assert stages["requested"].duration_seconds == 10
    assert stages["requested"].status == "completed"
    assert stages["running"].started_at == base + timedelta(seconds=40)
    assert stages["running"].ended_at is None
    assert stages["running"].duration_seconds == 30
    assert stages["running"].status == "active"
    assert stages["validating"].status == "pending"


@pytest.mark.unit
def test_lifecycle_summary_marks_future_stages_terminal_skipped() -> None:
    base = datetime(2026, 4, 27, 13, 0, tzinfo=UTC)
    failed_at = base + timedelta(seconds=75)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.failed,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.created",
                occurred_at=base,
                new_state=WorkspaceStatus.requested.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=25),
                old_state=WorkspaceStatus.provisioning.value,
                new_state=WorkspaceStatus.ready.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=40),
                old_state=WorkspaceStatus.ready.value,
                new_state=WorkspaceStatus.running.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=60),
                old_state=WorkspaceStatus.running.value,
                new_state=WorkspaceStatus.validating.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=failed_at,
                old_state=WorkspaceStatus.validating.value,
                new_state=WorkspaceStatus.failed.value,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=90),
        )
    }

    assert stages["validating"].ended_at == failed_at
    assert stages["validating"].duration_seconds == 15
    assert stages["validating"].status == "completed"
    assert stages["pushing"].status == "terminal_skipped"
    assert stages["monitoring_pr"].status == "terminal_skipped"
    assert stages["completed"].status == "terminal_skipped"


@pytest.mark.unit
def test_lifecycle_summary_marks_new_workspace_requested_active() -> None:
    base = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.requested,
        created_at=base,
        events=[],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=5),
        )
    }

    assert stages["requested"].started_at == base
    assert stages["requested"].ended_at is None
    assert stages["requested"].duration_seconds == 5
    assert stages["requested"].status == "active"
    assert stages["provisioning"].status == "pending"


@pytest.mark.unit
def test_lifecycle_summary_ignores_malformed_created_and_non_state_events() -> None:
    base = datetime(2026, 4, 27, 15, 0, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.requested,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.created",
                occurred_at=base + timedelta(seconds=10),
                new_state="not-a-workspace-state",
            ),
            _lifecycle_event(
                event_type="workspace.log_attached",
                occurred_at=base + timedelta(seconds=20),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=30),
                old_state=None,
                new_state=None,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=45),
        )
    }

    assert stages["requested"].started_at == base
    assert stages["requested"].duration_seconds == 45
    assert stages["requested"].status == "active"
    assert stages["provisioning"].status == "pending"


@pytest.mark.unit
def test_lifecycle_summary_closes_completed_stage_at_start_time() -> None:
    base = datetime(2026, 4, 27, 16, 0, tzinfo=UTC)
    completed_at = base + timedelta(seconds=90)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.completed,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.created",
                occurred_at=base,
                new_state=WorkspaceStatus.requested.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.monitoring_pr.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=completed_at,
                old_state=WorkspaceStatus.monitoring_pr.value,
                new_state=WorkspaceStatus.completed.value,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=completed_at + timedelta(seconds=30),
        )
    }

    assert stages["completed"].started_at == completed_at
    assert stages["completed"].ended_at == completed_at
    assert stages["completed"].duration_seconds == 0
    assert stages["completed"].status == "completed"


@pytest.mark.unit
def test_lifecycle_summary_uses_latest_started_stage_for_malformed_terminal_event() -> None:
    base = datetime(2026, 4, 27, 17, 0, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.failed,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.created",
                occurred_at=base,
                new_state=WorkspaceStatus.requested.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=20),
                old_state="unknown_state",
                new_state=WorkspaceStatus.failed.value,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=40),
        )
    }

    assert stages["provisioning"].started_at == base + timedelta(seconds=10)
    assert stages["provisioning"].ended_at is None
    assert stages["provisioning"].duration_seconds is None
    assert stages["provisioning"].status == "completed"
    assert stages["ready"].status == "terminal_skipped"


@pytest.mark.unit
def test_lifecycle_summary_tolerates_repeated_and_inferred_transitions() -> None:
    base = datetime(2026, 4, 27, 17, 30, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.running,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=10),
                old_state=WorkspaceStatus.ready.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=20),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=30),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.running.value,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=45),
        )
    }

    assert stages["ready"].started_at == base + timedelta(seconds=10)
    assert stages["ready"].ended_at == base + timedelta(seconds=10)
    assert stages["requested"].ended_at == base + timedelta(seconds=20)
    assert stages["provisioning"].started_at == base + timedelta(seconds=10)
    assert stages["running"].duration_seconds == 15
    assert stages["running"].status == "active"


@pytest.mark.unit
def test_lifecycle_summary_keeps_completed_stage_pending_without_completion_event() -> None:
    base = datetime(2026, 4, 27, 17, 45, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.completed,
        created_at=base,
        events=[],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=20),
        )
    }

    assert stages["requested"].status == "completed"
    assert stages["requested"].ended_at is None
    assert stages["requested"].duration_seconds is None
    assert stages["completed"].status == "pending"


@pytest.mark.unit
def test_lifecycle_terminal_fallback_prefers_latest_started_timestamp() -> None:
    base = datetime(2026, 4, 27, 17, 50, tzinfo=UTC)
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.failed,
        created_at=base,
        events=[
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=20),
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.validating.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=30),
                old_state="unknown_state",
                new_state=WorkspaceStatus.running.value,
            ),
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=base + timedelta(seconds=40),
                old_state=None,
                new_state=WorkspaceStatus.failed.value,
            ),
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=base + timedelta(seconds=60),
        )
    }

    assert stages["running"].started_at == base + timedelta(seconds=30)
    assert stages["running"].status == "completed"
    assert stages["validating"].started_at == base + timedelta(seconds=20)
    assert stages["validating"].status == "completed"
    assert stages["pushing"].status == "terminal_skipped"


@pytest.mark.unit
def test_lifecycle_summary_coerces_naive_and_offset_datetimes_to_utc() -> None:
    naive_base = datetime(2026, 4, 27, 18, 0)
    requested_end = datetime(
        2026,
        4,
        27,
        11,
        0,
        30,
        tzinfo=timezone(timedelta(hours=-7)),
    )
    workspace = _workspace_for_lifecycle(
        status=WorkspaceStatus.provisioning,
        created_at=naive_base,
        events=[
            _lifecycle_event(
                event_type="workspace.state_changed",
                occurred_at=requested_end,
                old_state=WorkspaceStatus.requested.value,
                new_state=WorkspaceStatus.provisioning.value,
            )
        ],
    )

    stages = {
        item.stage: item
        for item in workspace_lifecycle_summary(
            workspace,
            now=datetime(2026, 4, 27, 18, 1, tzinfo=UTC),
        )
    }

    assert stages["requested"].started_at == naive_base.replace(tzinfo=UTC)
    assert stages["requested"].ended_at == datetime(2026, 4, 27, 18, 0, 30, tzinfo=UTC)
    assert stages["requested"].duration_seconds == 30
    assert stages["provisioning"].started_at == datetime(
        2026,
        4,
        27,
        18,
        0,
        30,
        tzinfo=UTC,
    )
    assert stages["provisioning"].duration_seconds == 30


@pytest.mark.unit
def test_observability_payloads_include_identity_lifecycle_and_usage() -> None:
    base = datetime(2026, 4, 27, 19, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        id="ws_payload",
        agent=AgentRuntime.opencode.value,
        task_policy={"agent_effort": "max"},
        status=WorkspaceStatus.requested.value,
        created_at=base,
        events=[],
    )

    observability = workspace_observability_payload(
        workspace,
        now=base + timedelta(seconds=12),
    )
    identity_usage = workspace_identity_usage_payload(workspace)

    assert observability["agent_model"] == "ollama/kimi-k2.6:cloud"
    assert observability["agent_effort"] == "max"
    assert observability["agent_effort_source"] == "task_policy"
    assert observability["lifecycle"][0] == {
        "stage": "requested",
        "started_at": base,
        "ended_at": None,
        "duration_seconds": 12,
        "status": "active",
    }
    assert observability["llm_usage"]["status"] == "unavailable"
    assert identity_usage["agent_model"] == "ollama/kimi-k2.6:cloud"
    assert identity_usage["llm_usage"]["reason"] == "usage_not_reported"


@pytest.mark.unit
def test_workspace_usage_summary_is_explicitly_unavailable_without_adapter_usage() -> None:
    usage = workspace_usage_summary(SimpleNamespace(id="ws_usage"))

    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None
    assert usage.cost_estimate is None
    assert usage.currency is None
    assert usage.status == "unavailable"
    assert usage.source == "none"
    assert usage.reason == "usage_not_reported"
