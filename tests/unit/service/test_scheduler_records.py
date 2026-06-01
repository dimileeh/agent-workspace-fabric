"""Workspace service persistence for admission decisions and reservations."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service import workspaces, workspaces_create
from awf.service.disk import DiskCheck
from awf.service.workspaces import WorkspaceService
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _request() -> WorkspaceCreateRequest:
    return WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/service.git", "base_branch": "main"},
        task={
            "title": "Persist admission",
            "prompt": "Persist scheduler admission and reservation state.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "task_class": "dependency_task",
            "priority": 25,
            "owned_paths": ["pyproject.toml", "uv.lock"],
        },
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["uv run pytest -q"], "requested_tier": 2},
        resources={
            "steady_state_cpu_cores": 4.0,
            "steady_state_memory_gb": 12.0,
            "peak_cpu_cores": 8.0,
            "peak_memory_gb": 24.0,
            "disk_mb": 4096,
        },
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "scheduler test fixture",
        },
    )


def _request_with_companion_host_port(*, title: str, host_port: int) -> WorkspaceCreateRequest:
    data = _request().model_dump(mode="python")
    data["task"]["title"] = title
    data["companions"] = [
        {
            "name": "postgres",
            "repo_url": "https://github.com/example/postgres-companion",
            "ports": [[5432, host_port]],
        }
    ]
    return WorkspaceCreateRequest.model_validate(data)


def _dind_request() -> WorkspaceCreateRequest:
    data = _request().model_dump(mode="python")
    data["workspace"] = {
        "profile_ref": "inline",
        "profile": {
            "name": "local-dind",
            "docker": {"mode": "dind"},
        },
    }
    return WorkspaceCreateRequest.model_validate(data)


def _resource_request(
    *,
    title: str,
    steady_cpu: float,
    steady_memory_gb: float,
    peak_cpu: float,
    peak_memory_gb: float,
    disk_mb: int,
    dind: bool = False,
) -> WorkspaceCreateRequest:
    data = _request().model_dump(mode="python")
    data["task"]["title"] = title
    data["resources"] = {
        "steady_state_cpu_cores": steady_cpu,
        "steady_state_memory_gb": steady_memory_gb,
        "peak_cpu_cores": peak_cpu,
        "peak_memory_gb": peak_memory_gb,
        "disk_mb": disk_mb,
    }
    if dind:
        data["workspace"] = {
            "profile_ref": "inline",
            "profile": {
                "name": f"{title}-dind",
                "docker": {"mode": "dind"},
            },
        }
    return WorkspaceCreateRequest.model_validate(data)


def _disk_check() -> DiskCheck:
    return DiskCheck(
        path="/tmp/awf-work",
        checked_path="/tmp",
        total_bytes=20 * 1024 * 1024 * 1024,
        used_bytes=8 * 1024 * 1024 * 1024,
        free_bytes=12 * 1024 * 1024 * 1024,
        percent_free=60.0,
        threshold_bytes=1024,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
        detail=None,
    )


@pytest.mark.unit
async def test_create_writes_admitted_decision_and_local_reservation(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        local_capacity_cpu_cores=None,
        local_capacity_memory_gb=None,
        local_capacity_dind_slots=None,
    )
    monkeypatch.setattr(workspaces_create, "get_settings", lambda: settings)
    service = WorkspaceService(factory)

    created = await service.create(_request())

    async with factory() as session:
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(created.id)
        decisions = await QueueDecisionRepository(session).list_for_workspace(created.id)
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)

    assert attempt is not None
    assert len(decisions) == 1
    assert decisions[0].workspace_id == created.id
    assert decisions[0].task_id == attempt.task_id
    assert decisions[0].attempt_id == attempt.id
    assert decisions[0].decision == "admitted"
    assert decisions[0].reason_code == "ADMITTED_LOCAL"
    assert decisions[0].class_priority == 4
    assert decisions[0].computed_priority == 37
    assert decisions[0].age_boost == 0
    assert decisions[0].retry_bonus == 0
    assert decisions[0].score_summary["base_priority"] == 25
    assert decisions[0].score_summary["class_bias"] == 12
    assert decisions[0].score_summary["age_boost"] == 0
    assert decisions[0].score_summary["retry_bonus"] == 0
    assert decisions[0].score_summary["human_boost"] == 0
    assert decisions[0].score_summary["effective_score"] == 37
    assert decisions[0].score_summary["ordering_tuple"] == {
        "class_priority": 4,
        "effective_score": 37,
        "queued_at": decisions[0].score_summary["queued_at"],
        "workspace_id": created.id,
    }
    assert decisions[0].resource_summary == {
        "node_id": "local",
        "steady_cpu": 4.0,
        "steady_memory_gb": 12.0,
        "peak_cpu": 8.0,
        "peak_memory_gb": 24.0,
        "disk_mb": 4096,
        "dind_slots": 0,
        "phase": "workspace_lifecycle",
        "dind_mode": "unknown",
        "pressure_reasons": [],
        "capacity": {
            "steady_cpu": {
                "limit": None,
                "reserved": 4.0,
                "available": None,
                "available_after_next_default": None,
                "reason_code": "STEADY_CPU_CAPACITY_UNKNOWN",
            },
            "peak_cpu": {
                "limit": None,
                "reserved": 8.0,
                "available": None,
                "available_after_next_default": None,
                "reason_code": "PEAK_CPU_CAPACITY_UNKNOWN",
            },
            "steady_memory_gb": {
                "limit": None,
                "reserved": 12.0,
                "available": None,
                "available_after_next_default": None,
                "reason_code": "STEADY_MEMORY_CAPACITY_UNKNOWN",
            },
            "peak_memory_gb": {
                "limit": None,
                "reserved": 24.0,
                "available": None,
                "available_after_next_default": None,
                "reason_code": "PEAK_MEMORY_CAPACITY_UNKNOWN",
            },
            "disk_mb": {
                "limit": None,
                "reserved": 4096,
                "available": None,
                "available_after_next_default": None,
                "reason_code": "DISK_CAPACITY_UNKNOWN",
            },
            "dind_slots": {
                "limit": None,
                "reserved": 0,
                "available": None,
                "available_after_next_default": None,
                "reason_code": "DIND_CAPACITY_UNKNOWN",
            },
            "pressure_reasons": [
                "STEADY_CPU_CAPACITY_UNKNOWN",
                "PEAK_CPU_CAPACITY_UNKNOWN",
                "STEADY_MEMORY_CAPACITY_UNKNOWN",
                "PEAK_MEMORY_CAPACITY_UNKNOWN",
                "DISK_CAPACITY_UNKNOWN",
                "DIND_CAPACITY_UNKNOWN",
            ],
        },
    }
    assert decisions[0].overlap_risk_summary == {
        "warning_code": None,
        "overlap_count": 0,
        "workspace_ids": [],
        "overlaps": [],
    }

    assert len(reservations) == 1
    assert reservations[0].workspace_id == created.id
    assert reservations[0].attempt_id == attempt.id
    assert reservations[0].node_id == "local"
    assert reservations[0].steady_cpu == 4.0
    assert reservations[0].steady_memory_gb == 12.0
    assert reservations[0].peak_cpu == 8.0
    assert reservations[0].peak_memory_gb == 24.0
    assert reservations[0].disk_mb == 4096
    assert reservations[0].dind_slots == 0
    assert reservations[0].phase == "workspace_lifecycle"
    assert reservations[0].released_at is None


@pytest.mark.unit
async def test_create_rejects_host_port_conflict_on_configured_worker_node(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, worker_node_id="worker-host-1")
    host_port = 15432
    async with factory() as session:
        blocker = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/service.git",
            branch_base="main",
            task_title="existing port holder",
            task_prompt="already owns the companion port",
            agent="codex",
            task_policy={
                "companions": [
                    {
                        "name": "postgres",
                        "repo_url": "https://github.com/example/postgres-companion",
                        "ports": [[5432, host_port]],
                    }
                ]
            },
            test_commands=[],
        )
        blocker.status = WorkspaceStatus.running.value
        blocker.node_id = "worker-host-1"
        await session.commit()

    service = WorkspaceService(factory, settings=settings)

    with pytest.raises(workspaces.WorkspaceCreateHostPortConflictError) as exc_info:
        await service.create(
            _request_with_companion_host_port(
                title="new port holder",
                host_port=host_port,
            )
        )

    assert exc_info.value.host_port == host_port
    assert exc_info.value.conflicting_workspace_id == blocker.id


@pytest.mark.unit
async def test_create_writes_resource_summary_with_disk_dind_and_capacity(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        _env_file=None,
        local_capacity_cpu_cores=16.0,
        local_capacity_memory_gb=64.0,
        local_capacity_dind_slots=2,
    )
    async with factory() as session:
        created = await workspaces.create_workspace_row(
            session,
            _dind_request(),
            settings=settings,
            disk_check=_disk_check(),
        )
        await session.commit()

    async with factory() as session:
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(created.id)
        decisions = await QueueDecisionRepository(session).list_for_workspace(created.id)
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)

    assert attempt is not None
    assert len(decisions) == 1
    summary = decisions[0].resource_summary
    assert summary["node_id"] == "local"
    assert summary["steady_cpu"] == 4.0
    assert summary["peak_cpu"] == 8.0
    assert summary["steady_memory_gb"] == 12.0
    assert summary["peak_memory_gb"] == 24.0
    assert summary["disk_mb"] == 4096
    assert summary["dind_slots"] == 1
    assert summary["dind_mode"] == "dind"
    assert summary["pressure_reasons"] == []
    assert summary["capacity"]["peak_cpu"] == {
        "limit": 16.0,
        "reserved": 8.0,
        "available": 8.0,
        "available_after_next_default": 2.0,
        "reason_code": None,
    }
    assert summary["capacity"]["peak_memory_gb"] == {
        "limit": 64.0,
        "reserved": 24.0,
        "available": 40.0,
        "available_after_next_default": 24.0,
        "reason_code": None,
    }
    assert summary["capacity"]["disk_mb"] == {
        "limit": 20480,
        "reserved": 4096,
        "available": 8192,
        "available_after_next_default": None,
        "reason_code": None,
    }
    assert summary["capacity"]["dind_slots"] == {
        "limit": 2,
        "reserved": 1,
        "available": 1,
        "available_after_next_default": 1,
        "reason_code": None,
    }
    assert reservations[0].workspace_id == created.id
    assert reservations[0].attempt_id == attempt.id
    assert reservations[0].disk_mb == 4096
    assert reservations[0].dind_slots == 1
    assert reservations[0].released_at is None


@pytest.mark.unit
async def test_create_resource_summary_includes_existing_active_reservations(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        _env_file=None,
        local_capacity_cpu_cores=20.0,
        local_capacity_memory_gb=80.0,
        local_capacity_dind_slots=2,
    )
    async with factory() as session:
        await workspaces.create_workspace_row(
            session,
            _resource_request(
                title="active-one",
                steady_cpu=2.0,
                steady_memory_gb=4.0,
                peak_cpu=5.0,
                peak_memory_gb=10.0,
                disk_mb=2048,
            ),
            settings=settings,
            disk_check=_disk_check(),
        )
        await workspaces.create_workspace_row(
            session,
            _resource_request(
                title="active-two",
                steady_cpu=3.0,
                steady_memory_gb=14.0,
                peak_cpu=9.0,
                peak_memory_gb=50.0,
                disk_mb=6144,
                dind=True,
            ),
            settings=settings,
            disk_check=_disk_check(),
        )
        created = await workspaces.create_workspace_row(
            session,
            _dind_request(),
            settings=settings,
            disk_check=_disk_check(),
        )
        await session.commit()

    async with factory() as session:
        decisions = await QueueDecisionRepository(session).list_for_workspace(created.id)

    summary = decisions[0].resource_summary
    assert summary["capacity"]["steady_cpu"] == {
        "limit": 20.0,
        "reserved": 9.0,
        "available": 11.0,
        "available_after_next_default": 8.0,
        "reason_code": None,
    }
    assert summary["capacity"]["peak_cpu"] == {
        "limit": 20.0,
        "reserved": 22.0,
        "available": 0.0,
        "available_after_next_default": 0.0,
        "reason_code": "PEAK_CPU_CAPACITY_SATURATED",
    }
    assert summary["capacity"]["peak_memory_gb"] == {
        "limit": 80.0,
        "reserved": 84.0,
        "available": 0.0,
        "available_after_next_default": 0.0,
        "reason_code": "PEAK_MEMORY_CAPACITY_SATURATED",
    }
    assert summary["capacity"]["disk_mb"] == {
        "limit": 20480,
        "reserved": 12288,
        "available": 0,
        "available_after_next_default": None,
        "reason_code": "DISK_RESERVATION_PRESSURE",
    }
    assert summary["capacity"]["dind_slots"] == {
        "limit": 2,
        "reserved": 2,
        "available": 0,
        "available_after_next_default": 0,
        "reason_code": "DIND_CAPACITY_SATURATED",
    }
    assert summary["pressure_reasons"] == [
        "PEAK_CPU_CAPACITY_SATURATED",
        "PEAK_MEMORY_CAPACITY_SATURATED",
        "DISK_RESERVATION_PRESSURE",
        "DIND_CAPACITY_SATURATED",
    ]


@pytest.mark.unit
async def test_create_overlap_stays_advisory_with_resource_summary(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        first = await workspaces.create_workspace_row(session, _request())
        second = await workspaces.create_workspace_row(session, _request())
        await session.commit()

    async with factory() as session:
        decisions = await QueueDecisionRepository(session).list_for_workspace(second.id)

    assert first.id != second.id
    assert decisions[0].decision == "admitted"
    assert decisions[0].reason_code == "ADMITTED_LOCAL"
    assert decisions[0].resource_summary["disk_mb"] == 4096
    assert decisions[0].overlap_risk_summary["warning_code"] == "OWNED_PATH_OVERLAP_RISK"
    assert decisions[0].overlap_risk_summary["workspace_ids"] == [first.id]


@pytest.mark.unit
async def test_terminal_workspace_control_releases_active_reservation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async def noop_stopper(_compose_project_name: str | None) -> None:
        return None

    service = WorkspaceService(factory, project_stopper=noop_stopper)
    created = await service.create(_request())

    await service.cancel_workspace(
        created.id,
        reason="operator cancellation",
        stop_stack=False,
    )

    async with factory() as session:
        reservation = (await ResourceReservationRepository(session).list_for_workspace(created.id))[
            0
        ]
        active = await ResourceReservationRepository(session).active_for_workspace(created.id)

    assert active is None
    assert reservation.released_at is not None


@pytest.mark.unit
async def test_terminal_destroy_releases_leaked_active_reservation_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    created = await service.create(_request())

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.destroyed.value
        await session.commit()

    first = await service.destroy_workspace(
        created.id,
        force=True,
        remove_volumes=True,
        remove_worktree=True,
    )
    second = await service.destroy_workspace(
        created.id,
        force=True,
        remove_volumes=True,
        remove_worktree=True,
    )

    async with factory() as session:
        rows = await ResourceReservationRepository(session).list_for_workspace(created.id)

    assert first.status == WorkspaceStatus.destroyed
    assert second.status == WorkspaceStatus.destroyed
    assert rows[0].released_at is not None
    assert len({row.released_at for row in rows}) == 1


@pytest.mark.unit
def test_legacy_numeric_memory_without_unit_warns() -> None:
    from unittest.mock import patch

    warnings: list[tuple[str, dict[str, object]]] = []

    class Logger:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append((event, kwargs))

    with patch("awf.service.workspaces_create._log", Logger()):
        parsed = workspaces_create._parse_memory_gb("1024")

    assert parsed == 1024.0
    assert warnings == [
        (
            "workspace.resources.memory_unit_missing",
            {"raw_value": "1024", "interpreted_unit": "gb"},
        ),
    ]
