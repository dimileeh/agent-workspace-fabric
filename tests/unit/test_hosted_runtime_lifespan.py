"""Actual worker heartbeat and PostgreSQL admission drive runtime readiness."""

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_store import locked
from awf.hosted_node.runtime_worker import HostedWorker, start_runtime
from awf.service.resource_capacity import LocalCapacityLimits
from tests.unit.service.test_worker import _settings
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_capacity import configured


async def test_runtime_owns_real_worker_same_epoch_and_closes_when_stopped(
    authority, tmp_path, monkeypatch
):
    from awf.hosted_node import runtime_worker

    probe = Mock(return_value=LocalCapacityLimits(cpu_cores=16, memory_gb=64))
    monkeypatch.setattr(runtime_worker, "detect_local_capacity", probe)
    authority, _, factory = authority
    receiver = authority.receiver
    engine = factory.kw["bind"]
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    settings = replace(
        configured(tmp_path),
        database_url=url,
        github_token=None,
        work_dir=str(receiver.config.host.workspaces_root),
        worker_poll_interval_seconds=0.05,
        auto_cleanup_orphans=False,
        terminal_workspace_gc_enabled=False,
        claude_base_gc_enabled=False,
    )
    identity = receiver.identity
    async with start_runtime(receiver, settings) as runtime:
        for _ in range(60):
            if await runtime.is_ready(identity):
                break
            await asyncio.sleep(0.05)
        assert await runtime.is_ready(identity)
        assert probe.call_args.args == (settings,)
        probe.return_value = LocalCapacityLimits(cpu_cores=16, memory_gb=39)
        assert not await runtime.is_ready(identity)
        probe.return_value = LocalCapacityLimits(cpu_cores=16, memory_gb=64)
        assert receiver.identity == identity
        async with factory.begin() as session:
            row = await locked(session, identity)
            row.phase = "draining"
        assert not await runtime.is_ready(identity)
    assert runtime.task.done()
    assert not await runtime.is_ready(identity)


async def test_runtime_missing_capacity_never_starts_worker(authority, tmp_path, monkeypatch):
    from awf.hosted_node import runtime_worker

    authority, _, _ = authority
    build = Mock(side_effect=AssertionError("must not build"))
    monkeypatch.setattr(runtime_worker, "build_worker_runtime", build)
    monkeypatch.setattr(
        runtime_worker,
        "detect_local_capacity",
        Mock(return_value=LocalCapacityLimits(cpu_cores=16, memory_gb=64)),
    )
    settings = replace(
        _settings(tmp_path), work_dir=str(authority.receiver.config.host.workspaces_root)
    )
    with pytest.raises(MaterialDeniedError, match="HOSTED_CAPACITY_REQUIRED"):
        HostedWorker(authority.receiver, settings)
    build.assert_not_called()


async def test_runtime_rejects_unbound_work_root_and_observation_socket(
    authority, tmp_path, monkeypatch
):
    from awf.hosted_node import runtime_worker

    monkeypatch.setattr(
        runtime_worker, "build_worker_runtime", Mock(side_effect=AssertionError("must not build"))
    )
    authority, _, _ = authority
    receiver = authority.receiver
    settings = _settings(tmp_path)
    with pytest.raises(MaterialDeniedError, match="HOSTED_WORK_ROOT_MISMATCH"):
        HostedWorker(receiver, settings)
    settings = replace(
        settings,
        work_dir=str(receiver.config.host.workspaces_root),
        docker_host=f"unix://{receiver.config.host.docker_socket}",
    )
    with pytest.raises(MaterialDeniedError, match="EXECUTION_DOCKER_ENDPOINT_REQUIRED"):
        HostedWorker(receiver, settings)
