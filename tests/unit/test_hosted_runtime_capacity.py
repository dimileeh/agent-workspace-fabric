"""Hosted readiness cannot substitute a heartbeat for bounded physical capacity."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from awf.hosted_node.identity import MaterialDeniedError
from awf.node.compose_manager import ComposeManager, ComposeService, WorkspaceComposeSpec
from awf.service.resource_capacity import LocalCapacityLimits
from tests.unit.service.test_worker import _settings


def configured(tmp_path):
    return replace(
        _settings(tmp_path),
        local_capacity_cpu_cores=12,
        local_capacity_memory_gb=32,
        workspace_steady_cpu=3,
        workspace_steady_memory_gb=10,
        workspace_peak_cpu=6,
        workspace_peak_memory_gb=16,
        worker_max_concurrent_provisions=2,
        worker_max_concurrent_executions=2,
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"local_capacity_cpu_cores": None},
        {"local_capacity_memory_gb": None},
        {"local_capacity_cpu_cores": float("nan")},
        {"local_capacity_memory_gb": float("inf")},
        {"workspace_peak_cpu": 0},
        {"workspace_steady_cpu": 7},
        {"workspace_steady_memory_gb": 17},
        {"worker_max_concurrent_executions": 3},
        {"worker_max_concurrent_provisions": 3},
        {"worker_max_concurrent_executions": 0},
    ],
)
def test_capacity_rejects_missing_invalid_or_overcommitted_settings(tmp_path, updates):
    from awf.hosted_node.config import RuntimeCapacity
    from awf.hosted_node.runtime_capacity import validate_capacity

    with pytest.raises(MaterialDeniedError, match="HOSTED_CAPACITY"):
        validate_capacity(
            replace(configured(tmp_path), **updates),
            RuntimeCapacity(cpu_headroom_cores=4, memory_headroom_gb=8),
            LocalCapacityLimits(cpu_cores=16, memory_gb=64),
        )


@pytest.mark.parametrize(
    "cpu,memory", [(None, 64), (16, None), (15, 64), (16, 39), (float("inf"), 64)]
)
def test_capacity_requires_measured_cpu_and_memory_with_headroom(tmp_path, cpu, memory):
    from awf.hosted_node.config import RuntimeCapacity
    from awf.hosted_node.runtime_capacity import validate_capacity

    with pytest.raises(MaterialDeniedError, match="HOSTED_CAPACITY"):
        validate_capacity(
            configured(tmp_path),
            RuntimeCapacity(cpu_headroom_cores=4, memory_headroom_gb=8),
            LocalCapacityLimits(cpu_cores=cpu, memory_gb=memory),
        )


def test_capacity_requires_explicit_headroom_and_accepts_measured_budget(tmp_path):
    from awf.hosted_node.config import RuntimeCapacity
    from awf.hosted_node.runtime_capacity import validate_capacity

    measured = LocalCapacityLimits(cpu_cores=16, memory_gb=63.8)
    with pytest.raises(MaterialDeniedError, match="HOSTED_CAPACITY"):
        validate_capacity(configured(tmp_path), None, measured)
    validate_capacity(
        configured(tmp_path),
        RuntimeCapacity(cpu_headroom_cores=4, memory_headroom_gb=8),
        measured,
    )


def test_existing_broker_defers_third_default_flow_with_exact_hosted_budget(tmp_path):
    from awf.control.worker.config import WorkerConfig
    from awf.control.worker.resource_broker import (
        _AllocatedReservationTotals,
        _default_reservation_demand_for_workspace,
        _local_capacity_blockers,
    )

    settings = configured(tmp_path)
    config = WorkerConfig(
        local_capacity_cpu_cores=settings.local_capacity_cpu_cores,
        local_capacity_memory_gb=settings.local_capacity_memory_gb,
        workspace_steady_cpu=settings.workspace_steady_cpu,
        workspace_steady_memory_gb=settings.workspace_steady_memory_gb,
        workspace_peak_cpu=settings.workspace_peak_cpu,
        workspace_peak_memory_gb=settings.workspace_peak_memory_gb,
    )
    allocated = _AllocatedReservationTotals()
    for index in range(2):
        demand = _default_reservation_demand_for_workspace(
            f"ws_{index}", resolved_profile={"docker": {"mode": "none"}}, config=config
        )
        assert _local_capacity_blockers(allocated=allocated, demand=demand, config=config) == []
        allocated.add(demand)
    demand = _default_reservation_demand_for_workspace(
        "ws_third", resolved_profile={}, config=config
    )
    blockers = _local_capacity_blockers(allocated=allocated, demand=demand, config=config)
    assert {block.reason_code for block in blockers} == {
        "PEAK_CPU_CAPACITY_SATURATED",
        "PEAK_MEMORY_CAPACITY_SATURATED",
    }


def test_capacity_probe_uses_exact_execution_endpoint_without_host_docker_context(
    tmp_path, monkeypatch
):
    from awf.service.local_capacity import detect_local_capacity

    monkeypatch.setenv("DOCKER_CONTEXT", "unrelated-host")
    monkeypatch.setenv("GH_TOKEN", "synthetic-must-not-inherit")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"NCPU":16,"MemTotal":68719476736}', stderr="")

    measured = detect_local_capacity(
        configured(tmp_path),
        run_subprocess=run,
        environment={"PATH": "/usr/bin", "HOME": str(tmp_path / "empty")},
    )
    assert measured.cpu_cores == 16 and measured.memory_gb == 64
    assert calls[0][1]["env"] == {
        "PATH": "/usr/bin",
        "HOME": str(tmp_path / "empty"),
        "DOCKER_HOST": configured(tmp_path).docker_host,
    }


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "4"])
def test_headroom_is_positive_finite_explicit_numeric(value):
    from pydantic import ValidationError

    from awf.hosted_node.config import RuntimeCapacity

    with pytest.raises(ValidationError):
        RuntimeCapacity(cpu_headroom_cores=value, memory_headroom_gb=8)


@pytest.mark.parametrize("service_count", [0, 1, 2])
def test_hosted_limits_partition_reservation_across_every_container(tmp_path, service_count):
    template = Path(__file__).resolve().parents[2] / "docker/compose/workspace.base.yml.j2"
    manager = ComposeManager(work_dir=tmp_path, template_path=template)
    spec = WorkspaceComposeSpec(
        workspace_id="ws_capacity",
        worktree_host_path=tmp_path / "checkout",
        services=tuple(
            ComposeService(name=f"service{i}", image="postgres:16") for i in range(service_count)
        ),
        reservation_limits=(6.0, 16.0),
    )
    manifest = yaml.safe_load(manager.render(spec).compose_file.read_text())
    limits = [item["deploy"]["resources"]["limits"] for item in manifest["services"].values()]
    assert len(limits) == service_count + 1
    assert 0 < sum(float(item["cpus"]) for item in limits) <= 6
    assert 0 < sum(int(item["memory"]) for item in limits) <= 16 * 1024**3
    if service_count == 1:
        assert limits == [{"cpus": "3", "memory": "8589934592"}] * 2


def test_nonhosted_compose_has_no_new_implicit_limits(tmp_path):
    template = Path(__file__).resolve().parents[2] / "docker/compose/workspace.base.yml.j2"
    manager = ComposeManager(work_dir=tmp_path, template_path=template)
    spec = WorkspaceComposeSpec(
        workspace_id="ws_local",
        worktree_host_path=tmp_path / "checkout",
        services=(ComposeService(name="postgres", image="postgres:16"),),
    )
    manifest = yaml.safe_load(manager.render(spec).compose_file.read_text())
    assert all("deploy" not in item for item in manifest["services"].values())
    # Frozen unmodified ac3a8f880 local template, independent of shallow Git history.
    baseline = (
        Path(__file__).resolve().parents[1] / "fixtures/hosted_runtime/local_workspace.base.yml.j2"
    )
    old = ComposeManager(work_dir=tmp_path / "baseline", template_path=baseline)
    assert (
        manager.render(spec).compose_file.read_bytes() == old.render(spec).compose_file.read_bytes()
    )
