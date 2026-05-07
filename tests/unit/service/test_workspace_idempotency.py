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
) -> WorkspaceCreateV2Request:
    return WorkspaceCreateV2Request(
        repo={"url": "git@github.com:example/idempotency.git", "base_branch": "main"},
        task={
            "title": "Serialize v2 create",
            "prompt": "Exercise serialized idempotency lookup.",
            "agent": "codex",
            "kind": "feature_branch_pr",
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
async def test_create_v2_replay_conflicts_when_owned_path_payload_changes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create_v2(
        _v2_request(owned_paths=["src/awf/**", "tests/unit/**"]),
        idempotency_key="service-create-v2-owned-paths-order",
    )

    assert created.id.startswith("ws_")
    for changed_paths in (
        ["tests/unit/**", "src/awf/**"],
        ["src/awf/**"],
        ["src/awf/**", "tests/unit/**", "tests/unit/**"],
    ):
        with pytest.raises(WorkspaceCreateIdempotencyConflictError):
            await service.create_v2(
                _v2_request(owned_paths=changed_paths),
                idempotency_key="service-create-v2-owned-paths-order",
            )

    replayed = await service.create_v2(
        _v2_request(owned_paths=["src/awf/**", "tests/unit/**"]),
        idempotency_key="service-create-v2-owned-paths-order",
    )

    assert replayed.id == created.id


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
