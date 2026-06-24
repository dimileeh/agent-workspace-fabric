"""Resource saturation metrics API tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
from awf.api.routes import metrics as metrics_route
from awf.common.config import Settings, get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service import metrics as metrics_service
from awf.service.disk import DiskCheck
from awf.service.orphan_resources import (
    WorkspaceIdView,
    build_orphan_resource_summary,
    empty_docker_scan,
    scan_docker_resources,
    scan_managed_worktrees,
)
from awf.service.resource_capacity import LocalCapacityLimits


async def _workspace(
    engine: AsyncEngine,
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/metrics-api.git",
            branch_base="main",
            task_title=f"{status.value} workspace",
            task_prompt="Collect workspace reliability metrics.",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        await session.commit()
        return workspace.id


async def _workspace_with_reservation(
    engine: AsyncEngine,
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    steady_cpu: float,
    steady_memory_gb: float,
    peak_cpu: float,
    peak_memory_gb: float,
    disk_mb: int | None = None,
    dind_slots: int = 0,
    workspace_node_id: str | None = None,
    reservation_node_id: str = "local",
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/metrics-api.git",
            branch_base="main",
            task_title=f"{status.value} reserved workspace",
            task_prompt="Collect workspace reliability metrics.",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        workspace.node_id = workspace_node_id
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=None,
            idempotency_key=f"metrics-api-reservation:{workspace.id}",
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        await ResourceReservationRepository(session).create(
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            node_id=reservation_node_id,
            steady_cpu=steady_cpu,
            steady_memory_gb=steady_memory_gb,
            peak_cpu=peak_cpu,
            peak_memory_gb=peak_memory_gb,
            disk_mb=disk_mb,
            dind_slots=dind_slots,
            phase="workspace_lifecycle",
        )
        await session.commit()
        return workspace.id


def _disk_check(
    settings: Settings,
    *,
    ok: bool = True,
    free_bytes: int | None = None,
    reason: str = "SUFFICIENT_DISK",
) -> DiskCheck:
    threshold = settings.min_free_disk_bytes
    free = threshold + 1 if free_bytes is None else free_bytes
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=max(free + 1, threshold + 1),
        used_bytes=1,
        free_bytes=free,
        percent_free=99.0,
        threshold_bytes=threshold,
        ok=ok,
        status="ok" if ok else "fail",
        reason=reason,
        detail=None if ok else "Free disk is below the configured admission threshold.",
    )


def _empty_docker_run(args: list[str], **_kwargs: object) -> Any:
    if args[:3] in (
        ["docker", "ps", "-a"],
        ["docker", "network", "ls"],
        ["docker", "volume", "ls"],
    ):
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    raise AssertionError(f"unexpected subprocess call: {args}")


def _no_orphan_summary(settings: Settings, _session: Any) -> Any:
    return build_orphan_resource_summary(
        docker_scan=scan_docker_resources(
            docker_host=settings.docker_host,
            run_subprocess=_empty_docker_run,
        ),
        worktree_scan=scan_managed_worktrees(settings.work_dir),
        workspace_view=WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset(),
            available=True,
        ),
    )


@pytest.fixture
async def metrics_app_and_client(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Any, AsyncClient]]:
    monkeypatch.setenv("AWF_API_TOKEN", "unit-test-api-token")
    get_settings.cache_clear()
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    app.state.orphan_resource_summary_provider = _no_orphan_summary
    app.state.local_capacity_detector = lambda _settings: LocalCapacityLimits()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.headers["Authorization"] = "Bearer unit-test-api-token"
            yield app, c
    finally:
        get_settings.cache_clear()


@pytest.mark.unit
async def test_active_latest_totals_for_workspace_scope_delegates_to_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[WorkspaceStatus | str, ...] | None, str | None]] = []
    expected = {
        "workspace_count": 7,
        "steady_cpu": 1.0,
        "steady_memory_gb": 2.0,
        "peak_cpu": 3.0,
        "peak_memory_gb": 4.0,
        "disk_mb": 5,
        "dind_slots": 6,
    }

    async def _fake_totals(
        self: ResourceReservationRepository,
        *,
        statuses: tuple[WorkspaceStatus | str, ...] | None = None,
        node_id: str | None = None,
    ) -> dict[str, float | int]:
        del self
        calls.append((statuses, node_id))
        return expected

    class _NoSqlSession:
        async def execute(self, statement: object) -> object:
            del statement
            raise AssertionError("metrics should delegate aggregation to repository")

    monkeypatch.setattr(
        ResourceReservationRepository,
        "active_latest_totals_for_workspace_scope",
        _fake_totals,
        raising=False,
    )

    totals = await metrics_service._active_latest_totals_for_workspace_scope(  # noqa: SLF001
        _NoSqlSession(),  # type: ignore[arg-type]
        statuses=(WorkspaceStatus.requested,),
        node_id="worker-node-a",
    )

    assert totals == expected
    assert calls == [((WorkspaceStatus.requested,), "worker-node-a")]


@pytest.mark.unit
async def test_active_latest_totals_for_scheduler_allocation_scope_delegates_to_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[WorkspaceStatus | str, ...], str]] = []
    expected = {
        "workspace_count": 2,
        "steady_cpu": 3.0,
        "steady_memory_gb": 4.0,
        "peak_cpu": 5.0,
        "peak_memory_gb": 6.0,
        "disk_mb": 7,
        "dind_slots": 1,
    }

    async def _fake_totals(
        self: ResourceReservationRepository,
        *,
        statuses: tuple[WorkspaceStatus | str, ...],
        node_id: str,
    ) -> dict[str, float | int]:
        del self
        calls.append((statuses, node_id))
        return expected

    class _NoSqlSession:
        async def execute(self, statement: object) -> object:
            del statement
            raise AssertionError("metrics should delegate aggregation to repository")

    monkeypatch.setattr(
        ResourceReservationRepository,
        "active_latest_totals_for_scheduler_allocation_scope",
        _fake_totals,
        raising=False,
    )

    totals = await metrics_service._active_latest_totals_for_scheduler_allocation_scope(  # noqa: SLF001
        _NoSqlSession(),  # type: ignore[arg-type]
        statuses=(WorkspaceStatus.running,),
        node_id="worker-node-a",
    )

    assert totals == expected
    assert calls == [((WorkspaceStatus.running,), "worker-node-a")]


@pytest.mark.unit
async def test_resource_saturation_endpoint_uses_detected_docker_capacity_when_unset(
    metrics_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
        workspace_peak_cpu=6.0,
        workspace_peak_memory_gb=16.0,
        local_capacity_cpu_cores=None,
        local_capacity_memory_gb=None,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
        free_bytes=16 * 1024 * 1024 * 1024,
    )
    app.state.local_capacity_detector = lambda _settings: LocalCapacityLimits(
        cpu_cores=8.0,
        memory_gb=24.0,
        source="docker",
    )
    await _workspace(
        engine,
        status=WorkspaceStatus.running,
        updated_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    body = response.json()
    assert body["local_capacity"] == {
        "cpu_cores": 8.0,
        "memory_gb": 24.0,
        "source": "docker",
        "reason_code": None,
        "detail": None,
    }
    capacity = body["capacity"]
    assert capacity["peak_cpu"] == {
        "limit": 8.0,
        "reserved": 6.0,
        "available": 2.0,
        "available_after_next_default": 0.0,
        "reason_code": None,
    }
    assert capacity["peak_memory_gb"] == {
        "limit": 24.0,
        "reserved": 16.0,
        "available": 8.0,
        "available_after_next_default": 0.0,
        "reason_code": None,
    }


@pytest.mark.unit
async def test_resource_saturation_endpoint_tolerates_egress_count_failure(
    metrics_app_and_client: tuple[Any, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, client = metrics_app_and_client

    async def _raise_egress_counts(_session: object) -> dict[str, int]:
        raise RuntimeError("egress query failed")

    monkeypatch.setattr(metrics_route, "_egress_posture_counts", _raise_egress_counts)

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    assert response.json()["egress_posture_counts"] == {}


@pytest.mark.unit
async def test_resource_saturation_endpoint_reports_local_capacity_inputs(
    metrics_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
        worker_max_concurrent_provisions=5,
        worker_max_concurrent_executions=2,
        workspace_steady_cpu=2.0,
        workspace_steady_memory_gb=7.5,
        workspace_peak_cpu=4.0,
        workspace_peak_memory_gb=12.0,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
        free_bytes=16 * 1024 * 1024 * 1024,
    )
    now = datetime.now(UTC)
    for status in (
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.completed,
    ):
        await _workspace(engine, status=status, updated_at=now - timedelta(minutes=5))

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_counts"]["active_total"] == 3
    assert body["workspace_counts"]["ready"] == 1
    assert body["workspace_counts"]["running"] == 1
    assert body["workspace_counts"]["validating"] == 1
    assert body["workspace_counts"]["by_status"][WorkspaceStatus.completed.value] == 1
    assert body["worker"] == {
        "max_concurrent_provisions": 5,
        "max_concurrent_executions": 2,
    }
    assert body["resource_defaults"] == {
        "steady_cpu": 2.0,
        "steady_memory_gb": 7.5,
        "peak_cpu": 4.0,
        "peak_memory_gb": 12.0,
    }
    assert body["reserved_resources"] == {
        "active_workspace_count": 3,
        "steady_cpu": 6.0,
        "steady_memory_gb": 22.5,
        "peak_cpu": 12.0,
        "peak_memory_gb": 36.0,
        "disk_mb": 0,
        "dind_slots": 0,
    }
    assert body["concurrency"]["execution"] == {
        "limit": 2,
        "in_use": 2,
        "queued": 1,
        "available": 0,
    }
    assert body["disk"]["reason"] == "SUFFICIENT_DISK"
    assert body["orphan_resources"]["reason"] == "NO_ORPHANS"
    assert body["admission"]["ok"] is True
    assert body["admission"]["status"] == "saturated"
    assert body["admission"]["reason"] == "WORKER_EXECUTION_CONCURRENCY_SATURATED"
    assert body["local_capacity"] == {
        "cpu_cores": None,
        "memory_gb": None,
        "source": None,
        "reason_code": None,
        "detail": None,
    }


@pytest.mark.unit
async def test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure(
    metrics_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
        local_capacity_cpu_cores=8.0,
        local_capacity_memory_gb=24.0,
        local_capacity_dind_slots=1,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
        free_bytes=16 * 1024 * 1024 * 1024,
    )
    now = datetime.now(UTC)
    running_id = await _workspace_with_reservation(
        engine,
        status=WorkspaceStatus.running,
        updated_at=now - timedelta(minutes=5),
        steady_cpu=2.0,
        steady_memory_gb=4.0,
        peak_cpu=4.0,
        peak_memory_gb=8.0,
        dind_slots=1,
    )
    requested_id = await _workspace_with_reservation(
        engine,
        status=WorkspaceStatus.requested,
        updated_at=now - timedelta(minutes=4),
        steady_cpu=3.0,
        steady_memory_gb=8.0,
        peak_cpu=6.0,
        peak_memory_gb=16.0,
        dind_slots=1,
    )

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    body = response.json()
    assert body["local_capacity"] == {
        "cpu_cores": 8.0,
        "memory_gb": 24.0,
        "source": "operator_config",
        "reason_code": None,
        "detail": None,
    }
    assert body["reserved_resources"]["active_workspace_count"] == 2
    assert body["allocated_resources"] == {
        "active_workspace_count": 1,
        "steady_cpu": 2.0,
        "steady_memory_gb": 4.0,
        "peak_cpu": 4.0,
        "peak_memory_gb": 8.0,
        "disk_mb": 0,
        "dind_slots": 1,
    }
    assert body["allocated_capacity"]["peak_cpu"]["reserved"] == 4.0
    assert body["allocated_capacity"]["peak_cpu"]["available"] == 4.0
    assert body["capacity_queue"]["queued_workspace_count"] == 1
    assert body["capacity_queue"]["oldest_workspace_id"] == requested_id
    assert body["capacity_queue"]["oldest_wait_seconds"] >= 0
    assert body["capacity_queue"]["planned_resources"] == {
        "steady_cpu": 3.0,
        "steady_memory_gb": 8.0,
        "peak_cpu": 6.0,
        "peak_memory_gb": 16.0,
        "disk_mb": 0,
        "dind_slots": 1,
    }
    assert body["capacity_queue"]["blocked_reason_counts"] == {
        "DIND_CAPACITY_SATURATED": 1,
        "PEAK_CPU_CAPACITY_SATURATED": 1,
    }
    assert running_id != requested_id


@pytest.mark.unit
async def test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing(
    metrics_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
        worker_node_id="worker-node-a",
        local_capacity_cpu_cores=16.0,
        local_capacity_memory_gb=48.0,
        local_capacity_dind_slots=3,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
        free_bytes=16 * 1024 * 1024 * 1024,
    )
    now = datetime.now(UTC)
    await _workspace_with_reservation(
        engine,
        status=WorkspaceStatus.running,
        updated_at=now - timedelta(minutes=6),
        workspace_node_id="worker-node-a",
        reservation_node_id="worker-node-b",
        steady_cpu=2.0,
        steady_memory_gb=4.0,
        peak_cpu=6.0,
        peak_memory_gb=12.0,
        disk_mb=2048,
        dind_slots=1,
    )
    await _workspace_with_reservation(
        engine,
        status=WorkspaceStatus.running,
        updated_at=now - timedelta(minutes=5),
        workspace_node_id="worker-node-b",
        reservation_node_id="worker-node-a",
        steady_cpu=20.0,
        steady_memory_gb=40.0,
        peak_cpu=30.0,
        peak_memory_gb=60.0,
        disk_mb=8192,
        dind_slots=2,
    )
    requested_id = await _workspace_with_reservation(
        engine,
        status=WorkspaceStatus.requested,
        updated_at=now - timedelta(minutes=4),
        workspace_node_id="worker-node-a",
        reservation_node_id="worker-node-b",
        steady_cpu=3.0,
        steady_memory_gb=5.0,
        peak_cpu=4.0,
        peak_memory_gb=8.0,
        disk_mb=512,
        dind_slots=1,
    )
    await _workspace_with_reservation(
        engine,
        status=WorkspaceStatus.requested,
        updated_at=now - timedelta(minutes=3),
        workspace_node_id="worker-node-b",
        reservation_node_id="worker-node-a",
        steady_cpu=10.0,
        steady_memory_gb=20.0,
        peak_cpu=12.0,
        peak_memory_gb=24.0,
        disk_mb=4096,
        dind_slots=1,
    )

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_counts"]["active_total"] == 2
    assert body["reserved_resources"] == {
        "active_workspace_count": 2,
        "steady_cpu": 5.0,
        "steady_memory_gb": 9.0,
        "peak_cpu": 10.0,
        "peak_memory_gb": 20.0,
        "disk_mb": 2560,
        "dind_slots": 2,
    }
    assert body["allocated_resources"] == {
        "active_workspace_count": 1,
        "steady_cpu": 2.0,
        "steady_memory_gb": 4.0,
        "peak_cpu": 6.0,
        "peak_memory_gb": 12.0,
        "disk_mb": 2048,
        "dind_slots": 1,
    }
    assert body["capacity_queue"]["queued_workspace_count"] == 1
    assert body["capacity_queue"]["oldest_workspace_id"] == requested_id
    assert body["capacity_queue"]["planned_resources"] == {
        "steady_cpu": 3.0,
        "steady_memory_gb": 5.0,
        "peak_cpu": 4.0,
        "peak_memory_gb": 8.0,
        "disk_mb": 512,
        "dind_slots": 1,
    }


@pytest.mark.unit
async def test_resource_saturation_endpoint_serializes_orphan_resource_summary(
    metrics_app_and_client: tuple[Any, AsyncClient],
    tmp_path: Path,
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir=str(tmp_path),
        min_free_disk_bytes=700,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
        free_bytes=16 * 1024 * 1024 * 1024,
    )
    (tmp_path / "git" / "worktrees" / "ws_done").mkdir(parents=True)

    def _orphan_provider(provider_settings: Settings, _session: Any) -> Any:
        return build_orphan_resource_summary(
            docker_scan=scan_docker_resources(
                docker_host=provider_settings.docker_host,
                run_subprocess=_empty_docker_run,
            ),
            worktree_scan=scan_managed_worktrees(provider_settings.work_dir),
            workspace_view=WorkspaceIdView(
                active_ids=frozenset(),
                terminal_ids=frozenset({"ws_done"}),
                available=True,
            ),
        )

    app.state.orphan_resource_summary_provider = _orphan_provider

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    orphan_resources = response.json()["orphan_resources"]
    assert orphan_resources["ok"] is False
    assert orphan_resources["reason"] == "ORPHAN_RESOURCES_PRESENT"
    assert orphan_resources["orphan_count"] == 1
    assert orphan_resources["orphan_counts_by_kind"]["worktree"] == 1
    assert orphan_resources["cleanup_readiness"]["dry_run_only"] is True


@pytest.mark.unit
async def test_resource_saturation_endpoint_serializes_runtime_health_provider(
    metrics_app_and_client: tuple[Any, AsyncClient],
) -> None:
    from awf.service.workspace_runtime_health import (
        WorkspaceRuntimeFinding,
        WorkspaceRuntimeHealthSummary,
    )

    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
        free_bytes=16 * 1024 * 1024 * 1024,
    )

    def _runtime_health_provider(
        _settings: Settings,
        _session: Any,
        _orphan_resources: Any,
    ) -> WorkspaceRuntimeHealthSummary:
        return WorkspaceRuntimeHealthSummary(
            findings=(
                WorkspaceRuntimeFinding(
                    workspace_id="ws_missing_stack",
                    workspace_status=WorkspaceStatus.running.value,
                    status="stranded",
                    reason_code="STRANDED_WORKSPACE",
                    decision="fail_workspace",
                    message="No managed runtime containers were found.",
                    compose_project_name="awf_ws_missing_stack",
                ),
                WorkspaceRuntimeFinding(
                    workspace_id="ws_exited_agent",
                    workspace_status=WorkspaceStatus.running.value,
                    status="stranded",
                    reason_code="AGENT_CONTAINER_EXITED",
                    decision="fail_workspace",
                    message="Agent container is not running.",
                    compose_project_name="awf_ws_exited_agent",
                ),
                WorkspaceRuntimeFinding(
                    workspace_id="ws_recoverable_monitor",
                    workspace_status=WorkspaceStatus.monitoring_pr.value,
                    status="stranded",
                    reason_code="STRANDED_WORKSPACE",
                    decision="remonitor_workspace",
                    message="Monitoring PR workspace can be remonitored.",
                    compose_project_name="awf_ws_recoverable_monitor",
                ),
            )
        )

    app.state.runtime_health_summary_provider = _runtime_health_provider

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    runtime_health = response.json()["runtime_health"]
    assert runtime_health["stranded_count"] == 3
    assert runtime_health["fail_candidate_count"] == 2
    assert runtime_health["recoverable_count"] == 1
    assert runtime_health["reason_counts"] == {
        "AGENT_CONTAINER_EXITED": 1,
        "STRANDED_WORKSPACE": 2,
    }


@pytest.mark.unit
async def test_resource_saturation_runtime_health_provider_may_be_async(
    metrics_app_and_client: tuple[Any, AsyncClient],
) -> None:
    from awf.service.workspace_runtime_health import WorkspaceRuntimeHealthSummary

    app, client = metrics_app_and_client
    settings = Settings(_env_file=None, work_dir="/tmp/awf-metrics-work")
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
        free_bytes=16 * 1024 * 1024 * 1024,
    )

    async def _runtime_health_provider(
        _settings: Settings,
        _session: Any,
        _orphan_resources: Any,
    ) -> WorkspaceRuntimeHealthSummary:
        return WorkspaceRuntimeHealthSummary(findings=())

    app.state.runtime_health_summary_provider = _runtime_health_provider

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    assert response.json()["runtime_health"]["stranded_count"] == 0


@pytest.mark.unit
async def test_resource_saturation_orphan_provider_supports_async_and_db_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.api.routes.metrics as metrics_route

    settings = Settings(_env_file=None, work_dir=str(tmp_path))
    expected = _no_orphan_summary(settings, None)

    async def _async_provider(_settings: Settings, _session: Any) -> Any:
        return expected

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(orphan_resource_summary_provider=_async_provider))
    )
    provided = await metrics_route._resource_saturation_orphan_resources(
        request,
        settings,
        session=None,
    )

    class _BadSession:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("db down")

    monkeypatch.setattr(
        metrics_route, "scan_docker_resources", lambda **_kwargs: empty_docker_scan()
    )
    defaulted = await metrics_route._default_orphan_resource_summary(settings, _BadSession())

    assert provided is expected
    assert defaulted.reason == "DB_UNAVAILABLE"


@pytest.mark.unit
async def test_default_orphan_resource_summary_blocks_auto_cleanup_without_reaper_liveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metrics cannot promote orphan resources unless reaper liveness is proven."""

    settings = Settings(_env_file=None, work_dir=str(tmp_path), auto_cleanup_orphans=True)
    (tmp_path / "git" / "worktrees" / "ws_done").mkdir(parents=True)

    async def _terminal_view(_session: Any, *, min_retention_hours: float) -> WorkspaceIdView:
        return WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset({"ws_done"}),
            available=True,
        )

    monkeypatch.setattr(
        metrics_route, "scan_docker_resources", lambda **_kwargs: empty_docker_scan()
    )
    monkeypatch.setattr(metrics_route, "workspace_id_view_from_session", _terminal_view)

    summary = await metrics_route._default_orphan_resource_summary(settings, object())

    assert summary.ok is False
    assert summary.reason == "ORPHAN_RESOURCES_PRESENT"
    assert summary.cleanup_readiness.ready is False
    assert summary.cleanup_readiness.dry_run_only is True
    assert "Reaping is enabled" not in summary.cleanup_readiness.action


@pytest.mark.unit
async def test_resource_saturation_endpoint_uses_persisted_active_reservations(
    metrics_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
        workspace_steady_cpu=3.0,
        workspace_steady_memory_gb=10.0,
        workspace_peak_cpu=6.0,
        workspace_peak_memory_gb=16.0,
        local_capacity_cpu_cores=24.0,
        local_capacity_memory_gb=96.0,
        local_capacity_dind_slots=2,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
        free_bytes=16 * 1024 * 1024 * 1024,
    )
    now = datetime.now(UTC)
    await _workspace_with_reservation(
        engine,
        status=WorkspaceStatus.running,
        updated_at=now - timedelta(minutes=5),
        steady_cpu=4.0,
        steady_memory_gb=12.0,
        peak_cpu=8.0,
        peak_memory_gb=24.0,
        disk_mb=4096,
        dind_slots=1,
    )
    await _workspace(
        engine,
        status=WorkspaceStatus.ready,
        updated_at=now - timedelta(minutes=4),
    )

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_counts"]["active_total"] == 2
    assert body["reserved_resources"] == {
        "active_workspace_count": 2,
        "steady_cpu": 7.0,
        "steady_memory_gb": 22.0,
        "peak_cpu": 14.0,
        "peak_memory_gb": 40.0,
        "disk_mb": 4096,
        "dind_slots": 1,
    }
    assert body["capacity"]["peak_cpu"] == {
        "limit": 24.0,
        "reserved": 14.0,
        "available": 10.0,
        "available_after_next_default": 4.0,
        "reason_code": None,
    }
    assert body["capacity"]["peak_memory_gb"] == {
        "limit": 96.0,
        "reserved": 40.0,
        "available": 56.0,
        "available_after_next_default": 40.0,
        "reason_code": None,
    }
    assert body["capacity"]["disk_mb"]["reserved"] == 4096
    assert body["capacity"]["dind_slots"] == {
        "limit": 2,
        "reserved": 1,
        "available": 1,
        "available_after_next_default": 1,
        "reason_code": None,
    }
    assert body["capacity"]["pressure_reasons"] == []


@pytest.mark.unit
async def test_resource_saturation_endpoint_serializes_capacity_and_pressure(
    metrics_app_and_client: tuple[Any, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
        local_capacity_cpu_cores=8.0,
        local_capacity_memory_gb=20.0,
        local_capacity_dind_slots=1,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=True,
        free_bytes=2 * 1024 * 1024 * 1024,
    )
    now = datetime.now(UTC)
    await _workspace_with_reservation(
        engine,
        status=WorkspaceStatus.running,
        updated_at=now - timedelta(minutes=5),
        steady_cpu=4.0,
        steady_memory_gb=12.0,
        peak_cpu=9.0,
        peak_memory_gb=21.0,
        disk_mb=4096,
        dind_slots=1,
    )

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    body = response.json()
    assert body["reserved_resources"]["disk_mb"] == 4096
    assert body["reserved_resources"]["dind_slots"] == 1
    assert body["capacity"]["peak_cpu"]["reason_code"] == "PEAK_CPU_CAPACITY_SATURATED"
    assert body["capacity"]["peak_memory_gb"]["reason_code"] == ("PEAK_MEMORY_CAPACITY_SATURATED")
    assert body["capacity"]["disk_mb"]["reason_code"] == "DISK_RESERVATION_PRESSURE"
    assert body["capacity"]["dind_slots"]["reason_code"] == "DIND_CAPACITY_SATURATED"
    assert body["capacity"]["pressure_reasons"] == [
        "PEAK_CPU_CAPACITY_SATURATED",
        "PEAK_MEMORY_CAPACITY_SATURATED",
        "DISK_RESERVATION_PRESSURE",
        "DIND_CAPACITY_SATURATED",
    ]


@pytest.mark.unit
async def test_resource_saturation_endpoint_explains_disk_admission_denial(
    metrics_app_and_client: tuple[Any, AsyncClient],
) -> None:
    app, client = metrics_app_and_client
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-metrics-work",
        min_free_disk_bytes=700,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = lambda provider_settings: _disk_check(
        provider_settings,
        ok=False,
        free_bytes=100,
        reason="INSUFFICIENT_DISK",
    )

    response = await client.get("/v1/metrics/resources/saturation")

    assert response.status_code == 200
    body = response.json()
    assert body["disk"]["ok"] is False
    assert body["disk"]["reason"] == "INSUFFICIENT_DISK"
    assert body["admission"]["ok"] is False
    assert body["admission"]["status"] == "blocked"
    assert body["admission"]["reason"] == "INSUFFICIENT_DISK"
    assert "free disk" in body["admission"]["detail"].lower()


@pytest.mark.unit
def test_saturation_counts_response_carries_recovering() -> None:
    # The auto-healing provider-retry pause (#612) gets its own per-status
    # saturation count, mirroring ``blocked``. The response model maps it by
    # name from the ``WorkspaceSaturationCounts`` dataclass (``from_attributes``).
    from awf.service.metrics_types import WorkspaceSaturationCounts

    counts = WorkspaceSaturationCounts(
        by_status={WorkspaceStatus.recovering.value: 2},
        active_total=2,
        requested=0,
        provisioning=0,
        ready=0,
        running=0,
        validating=0,
        pushing=0,
        monitoring_pr=0,
        blocked=0,
        recovering=2,
        awaiting_human=0,
        destroying=0,
        completed=0,
        failed=0,
        cancelled=0,
        destroyed=0,
    )
    response = metrics_route.WorkspaceSaturationCountsResponse.model_validate(counts)
    assert response.recovering == 2
    assert response.active_total == 2
    assert response.model_dump()["recovering"] == 2


@pytest.mark.unit
def test_saturation_counts_response_carries_awaiting_human() -> None:
    # The PR-monitor HUMAN_WAIT escalation (#657) gets its own per-flag saturation
    # count alongside blocked/recovering. The response model maps it by name from
    # the ``WorkspaceSaturationCounts`` dataclass (``from_attributes``). The flag
    # lives on monitoring_pr rows, so it is already inside ``active_total``.
    from awf.service.metrics_types import WorkspaceSaturationCounts

    counts = WorkspaceSaturationCounts(
        by_status={WorkspaceStatus.monitoring_pr.value: 3},
        active_total=3,
        requested=0,
        provisioning=0,
        ready=0,
        running=0,
        validating=0,
        pushing=0,
        monitoring_pr=3,
        blocked=0,
        recovering=0,
        awaiting_human=2,
        destroying=0,
        completed=0,
        failed=0,
        cancelled=0,
        destroyed=0,
    )
    response = metrics_route.WorkspaceSaturationCountsResponse.model_validate(counts)
    assert response.awaiting_human == 2
    assert response.active_total == 3
    assert response.model_dump()["awaiting_human"] == 2
