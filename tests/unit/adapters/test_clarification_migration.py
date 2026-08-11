"""Persisted clarification model-network migration regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.adapters.clarification_migration import (
    PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
    PersistedClarificationModelNetworkAttachment,
    _attach_persisted_clarification_model_network,
    _rollback_persisted_clarification_model_network,
)
from awf.common.commands import COMMAND_TIMEOUT_REASON, FakeCommandRunner


def _assert_bounded_migration_calls(runner: FakeCommandRunner) -> None:
    """Assert each Docker call in the migration has its subprocess bound."""
    assert runner.calls
    assert all(
        call.timeout_seconds == PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS
        for call in runner.calls
    )


@pytest.mark.unit
async def test_attach_persisted_model_network_bounds_create_path_commands() -> None:
    """Network inspect, create, sidecar lookup, and connect cannot stall re-asks."""
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=1,
        stderr="Error response from daemon: network awf-ws_legacy-clarification-model-net not found",
    )
    runner.queue_result()
    runner.queue_result(stdout="stateful-model-container\n")
    runner.queue_result()

    _attachment, result = await _attach_persisted_clarification_model_network(
        runner,
        compose_project="awf_ws_legacy",
        compose_file=Path("/workspaces/ws_legacy/compose.yml"),
        workspace_id="ws_legacy",
        clarification_model_services=("ollama-sidecar",),
    )

    assert result.ok
    _assert_bounded_migration_calls(runner)


@pytest.mark.unit
async def test_attach_persisted_model_network_preserves_timeout_result() -> None:
    """A bounded Docker timeout remains visible to the adapter failure handler."""
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=124,
        stderr="command wall timeout after 30s\n",
        reason_code=COMMAND_TIMEOUT_REASON,
    )

    _attachment, result = await _attach_persisted_clarification_model_network(
        runner,
        compose_project="awf_ws_legacy",
        compose_file=Path("/workspaces/ws_legacy/compose.yml"),
        workspace_id="ws_legacy",
        clarification_model_services=("ollama-sidecar",),
    )

    assert result.reason_code == COMMAND_TIMEOUT_REASON
    _assert_bounded_migration_calls(runner)


@pytest.mark.unit
async def test_attach_persisted_model_network_bounds_alias_reconciliation_commands() -> None:
    """The idempotent alias reconnection operations cannot stall a re-ask."""
    runner = FakeCommandRunner()
    runner.queue_result()
    runner.queue_result(stdout="stateful-model-container\n")
    runner.queue_result(
        returncode=1,
        stderr="endpoint with name stateful-model-container already exists in network",
    )
    runner.queue_result()
    runner.queue_result()

    _attachment, result = await _attach_persisted_clarification_model_network(
        runner,
        compose_project="awf_ws_legacy",
        compose_file=Path("/workspaces/ws_legacy/compose.yml"),
        workspace_id="ws_legacy",
        clarification_model_services=("ollama-sidecar",),
    )

    assert result.ok
    _assert_bounded_migration_calls(runner)


@pytest.mark.unit
async def test_rollback_persisted_model_network_bounds_cleanup_commands() -> None:
    """Shielded network cleanup cannot wait forever on stalled Docker calls."""
    runner = FakeCommandRunner()
    runner.queue_result()
    runner.queue_result()
    runner.queue_result()
    attachment = PersistedClarificationModelNetworkAttachment(
        network_name="awf-ws_legacy-clarification-model-net",
        created_network=True,
        connected_container_ids=["new-model-container"],
        reconnecting_endpoints=[("existing-model-container", "ollama-sidecar")],
    )

    result = await _rollback_persisted_clarification_model_network(runner, attachment=attachment)

    assert result.ok
    _assert_bounded_migration_calls(runner)
