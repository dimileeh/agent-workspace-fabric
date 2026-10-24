"""The hosted path must compose the real local worker, not a second lifecycle."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from awf.control.executor import WorkspaceExecutor
from awf.control.worker import ControlWorker
from awf.hosted_node.runtime_consumers import HostedCommandRunner, WorkspaceConsumers
from awf.node.provisioner import Provisioner
from awf.runtime.driver import LocalRuntimeDriver
from awf.service import worker
from tests.unit.service.test_worker import _settings


async def test_real_worker_composition_is_identity_bound_and_has_no_local_auth_fallback(
    tmp_path, monkeypatch
):
    from awf.hosted_node.schemas import Identity

    authority = AsyncMock()
    authority.receiver.config.host = None
    authority.receiver.identity = Identity(
        owner="legacy/node",
        incarnation="123",
        recovery_generation=1,
        boot_epoch=2,
    )
    consumers = WorkspaceConsumers(authority, tmp_path)
    settings = replace(_settings(tmp_path), github_token=None)
    before = dict(__import__("os").environ)
    runtime = worker.build_worker_runtime(settings, hosted=consumers)
    try:
        assert isinstance(runtime.worker, ControlWorker)
        assert isinstance(runtime.runtime_driver, LocalRuntimeDriver)
        provisioner = runtime.worker._provisioner
        executor = runtime.worker._executor
        assert isinstance(provisioner, Provisioner)
        assert isinstance(executor, WorkspaceExecutor)
        assert provisioner._workspace_scope == consumers.scope
        assert executor._workspace_scope == consumers.scope
        assert isinstance(executor._runner, HostedCommandRunner)
        assert provisioner._before_provision is None
        assert provisioner._stack_launcher._auth_mount_resolver is None
        assert provisioner._stack_launcher._secret_lease_resolver is None
        factory = executor._session_factory
        assert (
            json.loads(factory.kw["info"]["awf_node_identity"])
            == authority.receiver.identity.model_dump()
        )
        assert before == dict(__import__("os").environ)
    finally:
        await runtime.engine.dispose()


async def test_normal_provisioner_and_executor_consumer_hooks_deny_before_work(
    tmp_path, monkeypatch
):
    from awf.hosted_node.identity import MaterialDeniedError
    from awf.hosted_node.schemas import Identity

    authority = AsyncMock()
    authority.receiver.config.host = None
    authority.receiver.identity = Identity(
        owner="owner", incarnation="123", recovery_generation=1, boot_epoch=2
    )
    authority.workspace.side_effect = MaterialDeniedError("GRANT_REVOKED")
    consumers = WorkspaceConsumers(authority, tmp_path)
    runtime = worker.build_worker_runtime(
        replace(_settings(tmp_path), github_token=None), hosted=consumers
    )
    # This wiring-only fixture has no database; the real cleanup callback is
    # exercised with PostgreSQL by test_hosted_runtime_review_retained.
    stopped = AsyncMock()
    consumers.stop_workspace = stopped
    try:
        for component, method, args in (
            (runtime.worker._provisioner, "provision_claimed", ("ws_target",)),
            (runtime.worker._executor, "execute", ("ws_target",)),
            (runtime.worker._executor, "resume_pr_monitor_handoff", ("ws_target",)),
            (runtime.worker._executor, "run_resumed_pr_monitor", ("ws_target", object())),
        ):
            with pytest.raises(MaterialDeniedError, match="GRANT_REVOKED"):
                await getattr(component, method)(*args)
        assert stopped.await_count == 4
        assert authority.retract.await_count == 4
        assert all(call.args == ("ws_target",) for call in stopped.await_args_list)
    finally:
        await runtime.engine.dispose()
