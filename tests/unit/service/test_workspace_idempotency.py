"""Workspace create idempotency serialization tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest, WorkspaceCreateV2Request
from awf.db.enums import AgentRuntime
from awf.db.models import Workspace
from awf.db.repositories import ResourceReservationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import DockerMode, ProfileDocker, ProfileResolution, WorkspaceProfile
from awf.service import workspaces
from awf.service.disk import DiskCheck
from awf.service.workspaces import WorkspaceCreateIdempotencyConflictError, WorkspaceService
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _v1_request() -> WorkspaceCreateRequest:
    return WorkspaceCreateRequest(
        repo_url="git@github.com:example/idempotency.git",
        branch_base="main",
        task_title="Serialize v1 create",
        task_prompt="Exercise serialized idempotency lookup.",
        agent=AgentRuntime.codex,
        test_commands=["pytest -q"],
        requires_database=False,
    )


def _v2_request(
    *,
    requested_tier: int = 1,
    resources: dict[str, object] | None = None,
    owned_paths: list[str] | None = None,
    profile_ref: str | None = "auto",
    priority: int = 0,
    human_boost: int = 0,
) -> WorkspaceCreateV2Request:
    return WorkspaceCreateV2Request(
        repo={"url": "git@github.com:example/idempotency.git", "base_branch": "main"},
        task={
            "title": "Serialize v2 create",
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


@pytest.mark.unit
async def test_create_locks_idempotency_key_before_lookup(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_idempotency_lock_order(monkeypatch)

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
async def test_create_v2_locks_idempotency_key_before_lookup(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_idempotency_lock_order(monkeypatch)

    created = await WorkspaceService(factory).create_v2(
        _v2_request(),
        idempotency_key="service-create-lock-v2",
    )

    assert created.id.startswith("ws_")
    assert calls[:2] == [
        ("lock", "service-create-lock-v2"),
        ("lookup", "service-create-lock-v2"),
    ]


@pytest.mark.unit
async def test_create_v2_resolves_lazy_disk_check_after_idempotency_replay(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    calls = 0

    async def disk_check_factory() -> DiskCheck:
        nonlocal calls
        calls += 1
        return _ok_disk_check()

    service = WorkspaceService(factory)
    created = await service.create_v2(
        _v2_request(),
        idempotency_key="service-create-v2-lazy-disk",
        disk_check_factory=disk_check_factory,
    )
    replayed = await service.create_v2(
        _v2_request(),
        idempotency_key="service-create-v2-lazy-disk",
        disk_check_factory=disk_check_factory,
    )

    assert replayed.id == created.id
    assert calls == 1


@pytest.mark.unit
async def test_create_v2_scheduler_replay_does_not_rebuild_full_task_policy(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkspaceService(factory)
    request = _v2_request(priority=42, human_boost=3)

    created = await service.create_v2(
        request,
        idempotency_key="service-create-v2-scheduler-policy",
    )

    def unexpected_snapshot(_: WorkspaceCreateV2Request) -> dict[str, object]:
        raise AssertionError("scheduler replay should not rebuild the full task policy")

    monkeypatch.setattr(workspaces, "v2_task_policy_snapshot", unexpected_snapshot)

    replayed = await service.create_v2(
        request,
        idempotency_key="service-create-v2-scheduler-policy",
    )

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_v2_auto_profile_replay_conflicts_with_matching_v1_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create(
        _v1_request(),
        idempotency_key="service-create-v1-then-v2-auto",
    )

    v2_payload = _v2_request().model_dump(mode="python")
    v2_payload["task"]["title"] = "Serialize v1 create"
    v2_payload["task"]["prompt"] = "Exercise serialized idempotency lookup."
    v2_payload["preflight"] = {}
    request = WorkspaceCreateV2Request.model_validate(v2_payload)

    assert created.id.startswith("ws_")
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create_v2(
            request,
            idempotency_key="service-create-v1-then-v2-auto",
        )


@pytest.mark.unit
async def test_create_v1_replay_conflicts_with_matching_v2_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    v2_payload = _v2_request().model_dump(mode="python")
    v2_payload["task"]["title"] = "Serialize v1 create"
    v2_payload["task"]["prompt"] = "Exercise serialized idempotency lookup."
    created = await service.create_v2(
        WorkspaceCreateV2Request.model_validate(v2_payload),
        idempotency_key="service-create-v2-then-v1-auto",
    )

    assert created.id.startswith("ws_")
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create(
            _v1_request(),
            idempotency_key="service-create-v2-then-v1-auto",
        )


@pytest.mark.unit
async def test_create_v2_auto_profile_replay_conflicts_when_requested_tier_changes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create_v2(
        _v2_request(requested_tier=1),
        idempotency_key="service-create-v2-tier",
    )

    assert created.id.startswith("ws_")
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create_v2(
            _v2_request(requested_tier=2),
            idempotency_key="service-create-v2-tier",
        )


@pytest.mark.unit
async def test_create_v2_auto_profile_legacy_replay_allows_missing_requested_tier(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    request = _v2_request(requested_tier=2)

    created = await service.create_v2(
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

    replayed = await service.create_v2(
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
async def test_create_v2_replay_compares_owned_paths_as_submitted_list(
    factory: async_sessionmaker[AsyncSession],
    case_name: str,
    initial_owned_paths: list[str],
    changed_owned_paths: list[str],
) -> None:
    service = WorkspaceService(factory)
    idempotency_key = f"service-create-v2-owned-paths-list-{case_name}"

    created = await service.create_v2(
        _v2_request(owned_paths=initial_owned_paths),
        idempotency_key=idempotency_key,
    )

    assert created.id.startswith("ws_")
    replayed = await service.create_v2(
        _v2_request(owned_paths=list(initial_owned_paths)),
        idempotency_key=idempotency_key,
    )
    assert replayed.id == created.id

    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create_v2(
            _v2_request(owned_paths=changed_owned_paths),
            idempotency_key=idempotency_key,
        )


@pytest.mark.unit
async def test_create_v2_named_profile_replay_uses_policy_tier_when_profile_unresolved(
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

    monkeypatch.setattr(workspaces, "resolve_workspace_profile", resolve_named_profile)
    request = _v2_request(requested_tier=2, profile_ref="high-perf")
    service = WorkspaceService(factory)

    created = await service.create_v2(
        request,
        idempotency_key="service-create-v2-named-profile-unresolved-tier",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        workspace.resolved_profile = None
        await session.commit()

    replayed = await service.create_v2(
        request,
        idempotency_key="service-create-v2-named-profile-unresolved-tier",
    )

    assert replayed.id == created.id
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create_v2(
            _v2_request(requested_tier=1, profile_ref="high-perf"),
            idempotency_key="service-create-v2-named-profile-unresolved-tier",
        )


@pytest.mark.unit
async def test_create_v2_named_profile_replay_prefers_policy_tier_over_stale_profile(
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

    monkeypatch.setattr(workspaces, "resolve_workspace_profile", resolve_named_profile)
    request = _v2_request(requested_tier=2, profile_ref="high-perf")
    service = WorkspaceService(factory)

    created = await service.create_v2(
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

    replayed = await service.create_v2(
        request,
        idempotency_key="service-create-v2-named-profile-policy-tier",
    )

    assert replayed.id == created.id
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create_v2(
            _v2_request(requested_tier=1, profile_ref="high-perf"),
            idempotency_key="service-create-v2-named-profile-policy-tier",
        )


@pytest.mark.unit
async def test_create_v2_replay_conflicts_when_resources_change(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create_v2(
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
        await service.create_v2(
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
async def test_create_v2_replay_ignores_absent_disk_request(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create_v2(
        _v2_request(),
        idempotency_key="service-create-v2-absent-disk",
    )
    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)
        reservations[0].disk_mb = 2048
        await session.commit()

    replayed = await service.create_v2(
        _v2_request(),
        idempotency_key="service-create-v2-absent-disk",
    )

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_v2_replay_uses_stored_resource_request_after_reservation_changes(
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

    created = await service.create_v2(
        request,
        idempotency_key="service-create-v2-default-then-explicit-resource",
    )
    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)
        reservations[0].steady_cpu = 99.0
        await session.commit()

    replayed = await service.create_v2(
        request,
        idempotency_key="service-create-v2-default-then-explicit-resource",
    )

    assert replayed.id == created.id
    with pytest.raises(WorkspaceCreateIdempotencyConflictError):
        await service.create_v2(
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
async def test_create_v2_persists_and_replays_empty_resource_snapshot(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkspaceService(factory)

    created = await service.create_v2(
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

    replayed = await service.create_v2(
        _v2_request(),
        idempotency_key="service-create-v2-empty-resource-snapshot",
    )

    assert replayed.id == created.id


@pytest.mark.unit
async def test_create_v2_named_profile_replay_preserves_stored_dind_mode(
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

    monkeypatch.setattr(workspaces, "resolve_workspace_profile", resolve_mutable_profile)
    data = _v2_request().model_dump(mode="python")
    data["workspace"] = {"profile_ref": "mutable-docker-mode", "profile": None}
    request = WorkspaceCreateV2Request.model_validate(data)
    service = WorkspaceService(factory)

    created = await service.create_v2(
        request,
        idempotency_key="service-create-v2-named-profile-dind-replay",
    )
    profile_mode = DockerMode.none

    replayed = await service.create_v2(
        request,
        idempotency_key="service-create-v2-named-profile-dind-replay",
    )

    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)

    assert replayed.id == created.id
    assert reservations[0].dind_slots == 1
