"""Workspace create idempotency serialization tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import AgentRuntime
from awf.db.models import TaskAttempt, Workspace
from awf.db.repositories import ResourceReservationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import DockerMode, ProfileDocker, ProfileResolution, WorkspaceProfile
from awf.service import workspaces, workspaces_create
from awf.service.disk import DiskCheck
from awf.service.workspaces import WorkspaceCreateIdempotencyConflictError, WorkspaceService
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _v1_request(*, requires_database: bool = False) -> WorkspaceCreateRequest:
    return WorkspaceCreateRequest(
        repo_url="git@github.com:example/idempotency.git",
        branch_base="main",
        task_title="Serialize v1 create",
        task_prompt="Exercise serialized idempotency lookup.",
        agent=AgentRuntime.codex,
        test_commands=["pytest -q"],
        requires_database=requires_database,
    )


def _v2_request(
    *,
    requested_tier: int = 1,
    resources: dict[str, object] | None = None,
    owned_paths: list[str] | None = None,
    profile_ref: str | None = "auto",
    priority: int = 0,
    human_boost: int = 0,
    companions: list[dict[str, object]] | None = None,
) -> WorkspaceCreateRequest:
    return WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/idempotency.git", "base_branch": "main"},
        task={
            "title": "Serialize rich create",
            "prompt": "Exercise serialized idempotency lookup.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "priority": priority,
            "human_boost": human_boost,
            "owned_paths": owned_paths or [],
        },
        workspace={"profile_ref": profile_ref, "profile": None},
        validation={"commands": ["pytest -q"], "requested_tier": requested_tier},
        resources=resources or {},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "idempotency serialization test fixture",
        },
        companions=companions or [],
    )


def _release_sync_request(
    *, auto_merge: bool = True, source_branch: str = "development"
) -> WorkspaceCreateRequest:
    return WorkspaceCreateRequest(
        repo={
            "url": "git@github.com:example/idempotency.git",
            "base_branch": "main",
            "source_branch": source_branch,
        },
        task={
            "title": "Sync release PR",
            "prompt": "Exercise serialized idempotency lookup.",
            "agent": "codex",
            "kind": "sync_release_pr",
            "auto_merge": auto_merge,
        },
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["pytest -q"], "requested_tier": 1},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "idempotency serialization test fixture",
        },
    )


def _record_idempotency_lock_order(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    original_get = WorkspaceRepository.get_by_idempotency_key

    async def record_lock(self: WorkspaceRepository, key: str) -> None:
        del self
        calls.append(("lock", key))

    async def assert_lookup_is_locked(
        self: WorkspaceRepository,
        key: str,
    ) -> Workspace | None:
        assert ("lock", key) in calls
        calls.append(("lookup", key))
        return await original_get(self, key)

    monkeypatch.setattr(WorkspaceRepository, "acquire_idempotency_key_lock", record_lock)
    monkeypatch.setattr(WorkspaceRepository, "get_by_idempotency_key", assert_lookup_is_locked)
    return calls


def _ok_disk_check() -> DiskCheck:
    return DiskCheck(
        path="/workspace",
        checked_path="/workspace",
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


async def _ready_provider_preflight(
    *_: object,
    agent: AgentRuntime | str,
    override: bool,
    override_reason: str | None,
    **__: object,
) -> dict[str, object]:
    normalized_reason = override_reason.strip() if override_reason is not None else None
    return {
        "provider": "codex",
        "agent": agent.value if isinstance(agent, AgentRuntime) else str(agent),
        "model": None,
        "model_source": "test",
        "readiness_status": "ready",
        "auth_status": "ok",
        "auth_source": "test",
        "credential_scope": "test",
        "isolation": "test",
        "probe_status": "ok",
        "reason_code": "PROVIDER_READINESS_READY",
        "message": "provider readiness satisfied by unit test fixture",
        "override_required": False,
        "override_requested": override,
        "override_used": False,
        "override_reason": normalized_reason or None,
        "blocks_launch": False,
        "checked_at": datetime(2026, 5, 17, tzinfo=UTC).isoformat(),
        "credential_sources": [],
    }


@pytest.mark.unit
async def test_resolve_disk_check_factory_accepts_sync_result() -> None:
    disk_check = _ok_disk_check()

    resolved = await workspaces._resolve_disk_check_factory(lambda: disk_check)  # noqa: SLF001

    assert resolved is disk_check


@pytest.mark.unit
def test_v2_idempotency_helper_edges_use_durable_request_snapshots() -> None:
    legacy_resources = _v2_request(resources={"cpu": 2.0, "memory": "4096mb"})

    assert workspaces._requested_resource_reservation_values(legacy_resources) == {  # noqa: SLF001
        "steady_cpu": 2.0,
        "peak_cpu": 2.0,
        "steady_memory_gb": 4.0,
        "peak_memory_gb": 4.0,
    }
    assert workspaces._owned_path_hints_match(["src/awf"], ["src/awf"])  # noqa: SLF001
    assert not workspaces._owned_path_hints_match(  # noqa: SLF001
        ["src/awf", "tests/unit"],
        ["tests/unit", "src/awf"],
    )
    assert not workspaces._owned_path_hints_match(["src/awf"], ["./src/awf"])  # noqa: SLF001
    assert workspaces._has_rich_create_artifact(SimpleNamespace(task_attempt=object()))  # noqa: SLF001
    assert not workspaces._has_rich_create_artifact(SimpleNamespace(task_attempt=None))  # noqa: SLF001
    orm_workspace = Workspace(
        id="ws_unloaded_rich_marker",
        status="requested",
        repo_url="git@github.com:example/idempotency.git",
        branch_base="main",
        task_title="Serialize rich create",
        task_prompt="Exercise serialized idempotency lookup.",
        agent="codex",
        test_commands=[],
    )
    assert not workspaces._has_rich_create_artifact(orm_workspace)  # noqa: SLF001
    orm_workspace.task_attempt = TaskAttempt(
        id="att_loaded_rich_marker",
        task_id="task_loaded_rich_marker",
        workspace_id=orm_workspace.id,
        attempt_number=1,
        agent="codex",
        status="requested",
        repo_url=orm_workspace.repo_url,
        base_branch=orm_workspace.branch_base,
        title=orm_workspace.task_title,
    )
    assert workspaces._has_rich_create_artifact(orm_workspace)  # noqa: SLF001
    assert not workspaces._profile_ref_matches(  # noqa: SLF001
        SimpleNamespace(
            profile_ref=None,
            requested_profile={"name": "inline"},
            task_attempt=object(),
        ),
        _v2_request(profile_ref="auto"),
    )
    assert not workspaces._legacy_validation_requested_tier_unknown(  # noqa: SLF001
        SimpleNamespace(
            profile_ref="python",
            requested_profile={"name": "inline"},
            resolved_profile=None,
        ),
        _v2_request(profile_ref="python"),
    )
    assert not workspaces._legacy_validation_requested_tier_unknown(  # noqa: SLF001
        SimpleNamespace(
            profile_ref="python",
            requested_profile=None,
            resolved_profile=None,
        ),
        _v2_request(profile_ref="node"),
    )


@pytest.mark.unit
def test_rich_resource_snapshot_helpers_reject_malformed_values() -> None:
    assert not workspaces._resource_reservation_matches_request_values(  # noqa: SLF001
        SimpleNamespace(
            steady_cpu=2.0,
            steady_memory_gb=4.0,
            peak_cpu=4.0,
            peak_memory_gb=8.0,
            disk_mb=1024,
        ),
        {"steady_cpu": 3.0},
    )
    assert (
        workspaces._stored_resource_reservation_request_values(  # noqa: SLF001
            SimpleNamespace(
                task_policy={
                    workspaces.RESOURCE_RESERVATION_REQUEST_POLICY_KEY: {"steady_cpu": True}
                }
            )
        )
        is None
    )
    assert (
        workspaces._stored_resource_reservation_request_values(  # noqa: SLF001
            SimpleNamespace(
                task_policy={workspaces.RESOURCE_RESERVATION_REQUEST_POLICY_KEY: {"disk_mb": 1.5}}
            )
        )
        is None
    )
    assert (
        workspaces._stored_resource_reservation_request_values(  # noqa: SLF001
            SimpleNamespace(
                task_policy={
                    workspaces.RESOURCE_RESERVATION_REQUEST_POLICY_KEY: {
                        "steady_memory_gb": "large"
                    }
                }
            )
        )
        is None
    )
    assert (
        workspaces._latest_workspace_resource_reservation(  # noqa: SLF001
            SimpleNamespace(
                resource_reservations=[
                    SimpleNamespace(id="older", reserved_at=datetime(2026, 1, 1, tzinfo=UTC)),
                    SimpleNamespace(id="newer", reserved_at=datetime(2026, 1, 2, tzinfo=UTC)),
                ]
            )
        ).id
        == "newer"
    )
    legacy_workspace_without_resource_snapshot = SimpleNamespace(
        task_policy={},
        resource_reservations=[],
        resolved_profile=None,
    )
    assert workspaces._stored_resource_reservation_matches(  # noqa: SLF001
        legacy_workspace_without_resource_snapshot,
        _v2_request(),
        settings=None,
    )
    assert (
        workspaces._stored_resource_reservation_matches(  # noqa: SLF001
            legacy_workspace_without_resource_snapshot,
            _v2_request(resources={"cpu": 2.0}),
            settings=None,
        )
        is False
    )


@pytest.mark.unit
async def test_v2_resource_snapshot_helpers_do_not_lazy_load_unloaded_reservations(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await WorkspaceService(factory).create(
        _v2_request(),
        idempotency_key="service-create-v2-unloaded-reservation-helper",
    )

    async with factory() as session:
        workspace = await session.get(Workspace, created.id)
        assert workspace is not None
        session.expire(workspace, ["resource_reservations"])

        assert workspaces._latest_workspace_resource_reservation(workspace) is None  # noqa: SLF001


@pytest.mark.unit
def test_v2_validation_tier_helper_falls_back_to_resolved_profile() -> None:
    assert (
        workspaces._stored_validation_requested_tier(  # noqa: SLF001
            SimpleNamespace(
                task_policy={},
                resolved_profile={"validation": {"requested_tier": 3}},
            )
        )
        == 3
    )


@pytest.mark.unit
async def test_create_locks_idempotency_key_before_lookup_for_legacy_payload(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_idempotency_lock_order(monkeypatch)
    monkeypatch.setattr(
        workspaces_create,
        "_selected_provider_preflight_for_task_async",
        _ready_provider_preflight,
    )

    created = await WorkspaceService(factory).create(
        _v1_request(),
        idempotency_key="service-create-lock-v1",
    )

    assert created.id.startswith("ws_")
    assert calls[:2] == [
        ("lock", "service-create-lock-v1"),
        ("lookup", "service-create-lock-v1"),
    ]


@pytest.mark.unit
async def test_create_locks_idempotency_key_before_lookup_for_rich_payload(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_idempotency_lock_order(monkeypatch)

    created = await WorkspaceService(factory).create(
        _v2_request(),
        idempotency_key="service-create-lock-v2",
    )

    assert created.id.startswith("ws_")
    assert calls[:2] == [
        ("lock", "service-create-lock-v2"),
        ("lookup", "service-create-lock-v2"),
    ]


@pytest.mark.unit
async def test_create_resolves_lazy_disk_check_after_idempotency_replay(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    calls = 0

    async def disk_check_factory() -> DiskCheck:
        nonlocal calls
        calls += 1
        return _ok_disk_check()

    service = WorkspaceService(factory)
    created = await service.create(
        _v2_request(),
        idempotency_key="service-create-v2-lazy-disk",
        disk_check_factory=disk_check_factory,
    )
    replayed = await service.create(
        _v2_request(),
        idempotency_key="service-create-v2-lazy-disk",
        disk_check_factory=disk_check_factory,
    )

    assert replayed.id == created.id
    assert calls == 1


@pytest.mark.unit
async def test_create_scheduler_replay_does_not_rebuild_full_task_policy(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkspaceService(factory)
    request = _v2_request(priority=42, human_boost=3)

    created = await service.create(
        request,
        idempotency_key="service-create-v2-scheduler-policy",
    )

    def unexpected_snapshot(_: WorkspaceCreateRequest) -> dict[str, object]:
        raise AssertionError("scheduler replay should not rebuild the full task policy")

    monkeypatch.setattr(workspaces, "workspace_create_task_policy_snapshot", unexpected_snapshot)

    replayed = await service.create(
        request,
        idempotency_key="service-create-v2-scheduler-policy",
    )

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_auto_profile_replays_matching_legacy_payload_row(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspaces_create,
        "_selected_provider_preflight_for_task_async",
        _ready_provider_preflight,
    )
    service = WorkspaceService(factory)

    created = await service.create(
        _v1_request(),
        idempotency_key="service-create-v1-then-v2-auto",
    )

    v2_payload = _v2_request().model_dump(mode="python")
    v2_payload["task"]["title"] = "Serialize v1 create"
    v2_payload["task"]["prompt"] = "Exercise serialized idempotency lookup."
    v2_payload["preflight"] = {}
    request = WorkspaceCreateRequest.model_validate(v2_payload)

    assert created.id.startswith("ws_")
    replayed = await service.create(
        request,
        idempotency_key="service-create-v1-then-v2-auto",
    )
    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_database_profile_replays_legacy_requires_database_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _v1_request(requires_database=True)
    idempotency_key = "service-create-v1-database-legacy-profile-replay"
    service = WorkspaceService(factory)

    async with factory() as session:
        created = await WorkspaceRepository(session).create(
            repo_url=request.repo.url,
            branch_base=request.repo.base_branch,
            task_title=request.task.title,
            task_prompt=request.task.prompt,
            task_external_id=request.task.external_id,
            agent=request.task.agent.value,
            env_profile=None,
            profile_ref=None,
            test_commands=request.validation.commands,
            requires_database=True,
            idempotency_key=idempotency_key,
        )
        await session.commit()

    non_database_payload = _v2_request(profile_ref="python").model_dump(mode="python")
    non_database_payload["task"]["title"] = request.task.title
    non_database_payload["task"]["prompt"] = request.task.prompt
    non_database_payload["preflight"] = {}
    non_database_request = WorkspaceCreateRequest.model_validate(non_database_payload)

    assert not workspaces.workspace_create_payload_matches(created, non_database_request)

    replayed = await service.create(request, idempotency_key=idempotency_key)

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_profile_ref_replays_legacy_env_profile_row_with_missing_requested_tier(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request_payload = _v2_request(requested_tier=2, profile_ref="python").model_dump(mode="python")
    request_payload["preflight"] = {}
    request = WorkspaceCreateRequest.model_validate(request_payload)
    idempotency_key = "service-create-v1-env-profile-legacy-tier"
    service = WorkspaceService(factory)

    async with factory() as session:
        created = await WorkspaceRepository(session).create(
            repo_url=request.repo.url,
            branch_base=request.repo.base_branch,
            task_title=request.task.title,
            task_prompt=request.task.prompt,
            task_external_id=request.task.external_id,
            agent=request.task.agent.value,
            env_profile="python",
            profile_ref=None,
            requested_profile=None,
            resolved_profile=None,
            test_commands=request.validation.commands,
            requires_database=False,
            idempotency_key=idempotency_key,
        )
        await session.commit()

    replayed = await service.create(request, idempotency_key=idempotency_key)

    assert replayed.id == created.id
    conflicting_payload = _v2_request(requested_tier=2, profile_ref="node").model_dump(
        mode="python"
    )
    conflicting_payload["preflight"] = {}
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            WorkspaceCreateRequest.model_validate(conflicting_payload),
            idempotency_key=idempotency_key,
        )


@pytest.mark.unit
async def test_create_v1_replay_conflicts_with_matching_v2_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    v2_payload = _v2_request().model_dump(mode="python")
    v2_payload["task"]["title"] = "Serialize v1 create"
    v2_payload["task"]["prompt"] = "Exercise serialized idempotency lookup."
    created = await service.create(
        WorkspaceCreateRequest.model_validate(v2_payload),
        idempotency_key="service-create-v2-then-v1-auto",
    )

    assert created.id.startswith("ws_")
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v1_request(),
            idempotency_key="service-create-v2-then-v1-auto",
        )


@pytest.mark.unit
async def test_create_auto_profile_replay_conflicts_when_requested_tier_changes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create(
        _v2_request(requested_tier=1),
        idempotency_key="service-create-v2-tier",
    )

    assert created.id.startswith("ws_")
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v2_request(requested_tier=2),
            idempotency_key="service-create-v2-tier",
        )


@pytest.mark.unit
async def test_create_replay_conflicts_when_agent_effort_changes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify effort mismatch invalidates workspace create idempotency replay."""
    service = WorkspaceService(factory)
    idempotency_key = "service-create-v2-agent-effort"
    request_payload = _v2_request().model_dump(mode="python")
    request_payload["task"]["effort"] = "high"
    request_with_high_effort = WorkspaceCreateRequest.model_validate(request_payload)

    created = await service.create(
        request_with_high_effort,
        idempotency_key=idempotency_key,
    )
    assert created.id.startswith("ws_")
    replayed = await service.create(
        request_with_high_effort,
        idempotency_key=idempotency_key,
    )
    assert replayed.id == created.id

    request_payload["task"]["effort"] = "xhigh"
    request_with_xhigh_effort = WorkspaceCreateRequest.model_validate(request_payload)
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            request_with_xhigh_effort,
            idempotency_key=idempotency_key,
        )


@pytest.mark.unit
async def test_create_release_sync_replay_matches_despite_forced_auto_merge_off(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Release-PR sync persists auto_merge=False; a same-payload replay (which
    still carries the request default auto_merge=True) must match instead of
    raising a spurious idempotency conflict."""
    service = WorkspaceService(factory)
    request = _release_sync_request(auto_merge=True)
    idempotency_key = "service-create-v2-release-sync-auto-merge"

    created = await service.create(request, idempotency_key=idempotency_key)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        assert workspace.auto_merge is False

    replayed = await service.create(request, idempotency_key=idempotency_key)

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_release_sync_legacy_replay_allows_stored_auto_merge_true(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A legacy sync_release_pr row written before auto_merge was forced off at
    persistence snapshotted the raw request default (auto_merge=True). An identical
    replay now resolves the effective auto_merge to False, so the matcher must skip
    the comparison rather than conflict on an otherwise unchanged request."""
    service = WorkspaceService(factory)
    idempotency_key = "service-create-v2-release-sync-legacy-auto-merge"
    request = _release_sync_request(auto_merge=True)

    created = await service.create(request, idempotency_key=idempotency_key)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        workspace.auto_merge = True
        await session.commit()

    replayed = await service.create(request, idempotency_key=idempotency_key)

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_release_sync_replay_conflicts_when_source_branch_changes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A sync_release_pr replay that changes repo.source_branch must conflict
    rather than silently reusing the workspace, which would sync the wrong branch."""
    service = WorkspaceService(factory)
    idempotency_key = "service-create-v2-release-sync-source-branch"
    request = _release_sync_request(source_branch="development")

    created = await service.create(request, idempotency_key=idempotency_key)
    assert created.id.startswith("ws_")

    replayed = await service.create(request, idempotency_key=idempotency_key)
    assert replayed.id == created.id

    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _release_sync_request(source_branch="release-candidate"),
            idempotency_key=idempotency_key,
        )


@pytest.mark.unit
async def test_create_release_sync_legacy_replay_allows_missing_source_branch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A legacy sync_release_pr row created before the source_branch snapshot has
    no recorded value; an identical default-branch replay must still match rather
    than conflict, because such rows could only have synced the default branch."""
    service = WorkspaceService(factory)
    idempotency_key = "service-create-v2-release-sync-legacy-source-branch"
    request = _release_sync_request(source_branch="development")

    created = await service.create(request, idempotency_key=idempotency_key)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        task_policy = dict(workspace.task_policy)
        task_policy.pop("release_sync", None)
        workspace.task_policy = task_policy
        await session.commit()

    replayed = await service.create(request, idempotency_key=idempotency_key)

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_auto_profile_legacy_replay_allows_missing_requested_tier(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    request = _v2_request(requested_tier=2)

    created = await service.create(
        request,
        idempotency_key="service-create-v2-legacy-auto-tier",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        task_policy = dict(workspace.task_policy)
        task_policy.pop("validation", None)
        workspace.task_policy = task_policy
        workspace.profile_ref = None
        workspace.resolved_profile = None
        await session.commit()

    replayed = await service.create(
        request,
        idempotency_key="service-create-v2-legacy-auto-tier",
    )

    assert replayed.id == created.id


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case_name", "initial_owned_paths", "changed_owned_paths"),
    [
        ("reordered", ["src/awf/**", "tests/unit/**"], ["tests/unit/**", "src/awf/**"]),
        ("duplicate_added", ["src/awf/**"], ["src/awf/**", "src/awf/**"]),
        ("deduped", ["src/awf/**", "src/awf/**"], ["src/awf/**"]),
        ("removed", ["src/awf/**", "tests/unit/**"], ["src/awf/**"]),
    ],
)
async def test_create_replay_compares_owned_paths_as_submitted_list(
    factory: async_sessionmaker[AsyncSession],
    case_name: str,
    initial_owned_paths: list[str],
    changed_owned_paths: list[str],
) -> None:
    service = WorkspaceService(factory)
    idempotency_key = f"service-create-v2-owned-paths-list-{case_name}"

    created = await service.create(
        _v2_request(owned_paths=initial_owned_paths),
        idempotency_key=idempotency_key,
    )

    assert created.id.startswith("ws_")
    replayed = await service.create(
        _v2_request(owned_paths=list(initial_owned_paths)),
        idempotency_key=idempotency_key,
    )
    assert replayed.id == created.id

    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v2_request(owned_paths=changed_owned_paths),
            idempotency_key=idempotency_key,
        )


@pytest.mark.unit
async def test_create_payload_match_treats_legacy_null_owned_paths_as_empty(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _v2_request(owned_paths=[])
    created = await WorkspaceService(factory).create(
        request,
        idempotency_key="service-create-v2-legacy-null-owned-paths",
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        workspace.owned_paths = None  # type: ignore[assignment]
        workspace.status = "running"

        assert workspaces.workspace_create_payload_matches(workspace, request)


@pytest.mark.unit
async def test_create_payload_match_treats_legacy_null_task_kind_as_feature_branch_pr(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _v2_request()
    created = await WorkspaceService(factory).create(
        request,
        idempotency_key="service-create-v2-legacy-null-task-kind",
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        workspace.task_kind = None  # type: ignore[assignment]
        workspace.status = "running"

        assert workspaces.workspace_create_payload_matches(workspace, request)


@pytest.mark.unit
async def test_create_replays_legacy_companion_without_environment_secrets(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _v2_request(
        companions=[
            {
                "name": "backend",
                "repo_url": "git@github.com:example/backend.git",
                "build_context": ".",
                "env_file": "config/dev.env",
            }
        ]
    )
    service = WorkspaceService(factory)
    created = await service.create(
        request,
        idempotency_key="service-create-v2-legacy-companion-env-secrets",
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        policy = dict(workspace.task_policy)
        stored_companion = dict(policy["companions"][0])
        assert stored_companion.pop("environment_secrets") == {}
        policy["companions"] = [stored_companion]
        workspace.task_policy = policy
        await session.commit()

    replayed = await service.create(
        request,
        idempotency_key="service-create-v2-legacy-companion-env-secrets",
    )

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_named_profile_replay_uses_policy_tier_when_profile_unresolved(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve_named_profile(**_: object) -> ProfileResolution:
        return ProfileResolution(
            profile=WorkspaceProfile(
                name="high-perf",
                source="test:high-perf",
            ),
            network_posture="restricted",
            reason="test profile fixture",
            candidates_considered=["registry:high-perf"],
        )

    monkeypatch.setattr(workspaces_create, "resolve_workspace_profile", resolve_named_profile)
    request = _v2_request(requested_tier=2, profile_ref="high-perf")
    service = WorkspaceService(factory)

    created = await service.create(
        request,
        idempotency_key="service-create-v2-named-profile-unresolved-tier",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        workspace.resolved_profile = None
        await session.commit()

    replayed = await service.create(
        request,
        idempotency_key="service-create-v2-named-profile-unresolved-tier",
    )

    assert replayed.id == created.id
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v2_request(requested_tier=1, profile_ref="high-perf"),
            idempotency_key="service-create-v2-named-profile-unresolved-tier",
        )


@pytest.mark.unit
async def test_create_named_profile_legacy_replay_allows_missing_unresolved_tier(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve_named_profile(**_: object) -> ProfileResolution:
        return ProfileResolution(
            profile=WorkspaceProfile(
                name="high-perf",
                source="test:high-perf",
            ),
            network_posture="restricted",
            reason="test profile fixture",
            candidates_considered=["registry:high-perf"],
        )

    monkeypatch.setattr(workspaces_create, "resolve_workspace_profile", resolve_named_profile)
    request = _v2_request(requested_tier=2, profile_ref="high-perf")
    service = WorkspaceService(factory)

    created = await service.create(
        request,
        idempotency_key="service-create-v2-legacy-named-profile-tier",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        task_policy = dict(workspace.task_policy)
        task_policy.pop("validation", None)
        workspace.task_policy = task_policy
        workspace.resolved_profile = None
        await session.commit()

    replayed = await service.create(
        request,
        idempotency_key="service-create-v2-legacy-named-profile-tier",
    )

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_named_profile_replay_prefers_policy_tier_over_stale_profile(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve_named_profile(**_: object) -> ProfileResolution:
        return ProfileResolution(
            profile=WorkspaceProfile(
                name="high-perf",
                source="test:high-perf",
            ),
            network_posture="restricted",
            reason="test profile fixture",
            candidates_considered=["registry:high-perf"],
        )

    monkeypatch.setattr(workspaces_create, "resolve_workspace_profile", resolve_named_profile)
    request = _v2_request(requested_tier=2, profile_ref="high-perf")
    service = WorkspaceService(factory)

    created = await service.create(
        request,
        idempotency_key="service-create-v2-named-profile-policy-tier",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        assert workspace.task_policy["validation"]["requested_tier"] == 2
        resolved_profile = dict(workspace.resolved_profile or {})
        validation = dict(resolved_profile.get("validation") or {})
        validation["requested_tier"] = 1
        resolved_profile["validation"] = validation
        workspace.resolved_profile = resolved_profile
        await session.commit()

    replayed = await service.create(
        request,
        idempotency_key="service-create-v2-named-profile-policy-tier",
    )

    assert replayed.id == created.id
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v2_request(requested_tier=1, profile_ref="high-perf"),
            idempotency_key="service-create-v2-named-profile-policy-tier",
        )


@pytest.mark.unit
async def test_create_replay_conflicts_when_resources_change(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create(
        _v2_request(
            resources={
                "steady_state_cpu_cores": 2.0,
                "steady_state_memory_gb": 6.0,
                "peak_cpu_cores": 4.0,
                "peak_memory_gb": 12.0,
                "disk_mb": 2048,
            }
        ),
        idempotency_key="service-create-v2-resources",
    )

    assert created.id.startswith("ws_")
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v2_request(
                resources={
                    "steady_state_cpu_cores": 3.0,
                    "steady_state_memory_gb": 6.0,
                    "peak_cpu_cores": 4.0,
                    "peak_memory_gb": 12.0,
                    "disk_mb": 2048,
                }
            ),
            idempotency_key="service-create-v2-resources",
        )


@pytest.mark.unit
async def test_create_replay_uses_stored_resource_request_when_reservation_is_capped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    idempotency_key = "service-create-v2-resources-capped-reservation"
    request = _v2_request(resources={"steady_state_cpu_cores": 2.0})

    created = await service.create(request, idempotency_key=idempotency_key)
    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)
        reservations[0].steady_cpu = 1.5
        await session.commit()

    replayed = await service.create(request, idempotency_key=idempotency_key)

    assert replayed.id == created.id
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v2_request(resources={"steady_state_cpu_cores": 1.5}),
            idempotency_key=idempotency_key,
        )


@pytest.mark.unit
async def test_create_replay_conflicts_when_resources_change_without_reservation_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create(
        _v2_request(
            resources={
                "steady_state_cpu_cores": 2.0,
                "steady_state_memory_gb": 6.0,
                "peak_cpu_cores": 4.0,
                "peak_memory_gb": 12.0,
                "disk_mb": 2048,
            }
        ),
        idempotency_key="service-create-v2-missing-reservation-resources",
    )
    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)
        assert len(reservations) == 1
        await session.delete(reservations[0])
        await session.commit()

    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v2_request(
                resources={
                    "steady_state_cpu_cores": 3.0,
                    "steady_state_memory_gb": 6.0,
                    "peak_cpu_cores": 4.0,
                    "peak_memory_gb": 12.0,
                    "disk_mb": 2048,
                }
            ),
            idempotency_key="service-create-v2-missing-reservation-resources",
        )


@pytest.mark.unit
async def test_create_replay_ignores_absent_disk_request(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create(
        _v2_request(),
        idempotency_key="service-create-v2-absent-disk",
    )
    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)
        reservations[0].disk_mb = 2048
        await session.commit()

    replayed = await service.create(
        _v2_request(),
        idempotency_key="service-create-v2-absent-disk",
    )

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_replay_uses_stored_resource_request_after_reservation_changes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    request = _v2_request(
        resources={
            "steady_state_cpu_cores": 2.0,
            "steady_state_memory_gb": 6.0,
            "peak_cpu_cores": 4.0,
            "peak_memory_gb": 12.0,
            "disk_mb": 2048,
        }
    )

    created = await service.create(
        request,
        idempotency_key="service-create-v2-default-then-explicit-resource",
    )
    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)
        reservations[0].steady_cpu = 99.0
        await session.commit()

    replayed = await service.create(
        request,
        idempotency_key="service-create-v2-default-then-explicit-resource",
    )

    assert replayed.id == created.id
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v2_request(
                resources={
                    "steady_state_cpu_cores": 3.0,
                    "steady_state_memory_gb": 6.0,
                    "peak_cpu_cores": 4.0,
                    "peak_memory_gb": 12.0,
                    "disk_mb": 2048,
                }
            ),
            idempotency_key="service-create-v2-default-then-explicit-resource",
        )


@pytest.mark.unit
async def test_create_legacy_replay_allows_explicit_resource_matching_prior_default(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create(
        _v2_request(),
        idempotency_key="service-create-v2-legacy-default-then-explicit-resource",
    )
    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)
        steady_cpu = reservations[0].steady_cpu
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        task_policy = dict(workspace.task_policy)
        task_policy.pop(workspaces.RESOURCE_RESERVATION_REQUEST_POLICY_KEY, None)
        workspace.task_policy = task_policy
        await session.commit()

    replayed = await service.create(
        _v2_request(resources={"steady_state_cpu_cores": steady_cpu}),
        idempotency_key="service-create-v2-legacy-default-then-explicit-resource",
    )

    assert replayed.id == created.id
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v2_request(resources={"steady_state_cpu_cores": steady_cpu + 1.0}),
            idempotency_key="service-create-v2-legacy-default-then-explicit-resource",
        )


@pytest.mark.unit
async def test_create_persists_and_replays_empty_resource_snapshot(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkspaceService(factory)

    created = await service.create(
        _v2_request(),
        idempotency_key="service-create-v2-empty-resource-snapshot",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        assert workspace.task_policy[workspaces.RESOURCE_RESERVATION_REQUEST_POLICY_KEY] == {}

    def unexpected_plan(*_: object, **__: object) -> object:
        raise AssertionError("empty stored resource snapshots should not re-plan")

    monkeypatch.setattr(workspaces, "resource_reservation_plan", unexpected_plan)

    replayed = await service.create(
        _v2_request(),
        idempotency_key="service-create-v2-empty-resource-snapshot",
    )

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_named_profile_replay_preserves_stored_dind_mode(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_mode = DockerMode.dind

    def resolve_mutable_profile(**_: object) -> ProfileResolution:
        return ProfileResolution(
            profile=WorkspaceProfile(
                name="mutable-docker-mode",
                source=f"test:{profile_mode.value}",
                docker=ProfileDocker(mode=profile_mode),
            ),
            network_posture="restricted",
            reason="test profile fixture",
            candidates_considered=["registry:mutable-docker-mode"],
        )

    monkeypatch.setattr(workspaces_create, "resolve_workspace_profile", resolve_mutable_profile)
    data = _v2_request().model_dump(mode="python")
    data["workspace"] = {"profile_ref": "mutable-docker-mode", "profile": None}
    request = WorkspaceCreateRequest.model_validate(data)
    service = WorkspaceService(factory)

    created = await service.create(
        request,
        idempotency_key="service-create-v2-named-profile-dind-replay",
    )
    profile_mode = DockerMode.none

    replayed = await service.create(
        request,
        idempotency_key="service-create-v2-named-profile-dind-replay",
    )

    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)

    assert replayed.id == created.id
    assert reservations[0].dind_slots == 1


@pytest.mark.unit
async def test_create_named_profile_replay_uses_stored_resource_snapshot(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve_named_profile(**_: object) -> ProfileResolution:
        return ProfileResolution(
            profile=WorkspaceProfile(
                name="mutable-dind",
                source="test:mutable-dind",
                docker=ProfileDocker(mode=DockerMode.dind),
            ),
            network_posture="restricted",
            reason="test profile fixture",
            candidates_considered=["registry:mutable-dind"],
        )

    monkeypatch.setattr(workspaces_create, "resolve_workspace_profile", resolve_named_profile)
    request = _v2_request(profile_ref="mutable-dind")
    service = WorkspaceService(factory)

    created = await service.create(
        request,
        idempotency_key="service-create-v2-named-profile-stored-resource-snapshot",
    )

    def fail_profile_resolution(**_: object) -> ProfileResolution:
        raise AssertionError("idempotency replay should not resolve mutable profiles")

    monkeypatch.setattr(workspaces_create, "resolve_workspace_profile", fail_profile_resolution)

    replayed = await service.create(
        request,
        idempotency_key="service-create-v2-named-profile-stored-resource-snapshot",
    )

    assert replayed.id == created.id
