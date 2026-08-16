"""Persisted clarification model-network migration regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.adapters.clarification_migration import (
    PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
    PersistedClarificationModelNetworkAttachment,
    _attach_persisted_clarification_model_network,
    _RepairedClarificationModelNetworkEndpoint,
    _rollback_persisted_clarification_model_network,
)
from awf.common.commands import COMMAND_TIMEOUT_REASON, CommandResult, FakeCommandRunner


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
async def test_attach_persisted_model_network_preserves_existing_aliased_endpoint() -> None:
    """An overlapping re-ask must not detach a sidecar that already has its alias."""
    network_name = "awf-ws_legacy-clarification-model-net"
    container_id = "stateful-model-container"
    runner = FakeCommandRunner()
    runner.queue_result(stdout=f"{container_id}\n")
    runner.queue_result(stdout=f"{container_id}\n")
    runner.queue_result(returncode=1, stderr="endpoint already connected")
    runner.queue_result(stdout="ollama-sidecar\n")

    attachment, result = await _attach_persisted_clarification_model_network(
        runner,
        compose_project="awf_ws_legacy",
        compose_file=Path("/workspaces/ws_legacy/compose.yml"),
        workspace_id="ws_legacy",
        clarification_model_services=("ollama-sidecar",),
    )

    assert result.ok
    assert attachment.reconnecting_endpoints == []
    assert [call.args[:3] for call in runner.calls] == [
        ["docker", "network", "inspect"],
        ["docker", "compose", "-p"],
        ["docker", "network", "connect"],
        ["docker", "inspect", "--format"],
    ]
    assert runner.calls[-1].args == [
        "docker",
        "inspect",
        "--format",
        (
            f'{{{{ with index .NetworkSettings.Networks "{network_name}" }}}}'
            r'{{ range .Aliases }}{{ printf "%s\n" . }}{{ end }}'
            r"{{ end }}"
        ),
        container_id,
    ]
    assert not any(call.args[2] == "disconnect" for call in runner.calls if len(call.args) > 2)
    _assert_bounded_migration_calls(runner)


@pytest.mark.unit
async def test_rollback_persisted_model_network_restores_repaired_endpoint_aliases() -> None:
    """A failed migration restores aliases displaced by endpoint repair."""
    network_name = "awf-ws_legacy-clarification-model-net"
    container_id = "stateful-model-container"
    runner = FakeCommandRunner()
    runner.queue_result(stdout=f"{container_id}\n")
    runner.queue_result(stdout=f"{container_id}\n")
    runner.queue_result(returncode=1, stderr="endpoint already connected")
    runner.queue_result(stdout="legacy-primary\nlegacy-secondary\n")
    runner.queue_result()
    runner.queue_result()

    attachment, result = await _attach_persisted_clarification_model_network(
        runner,
        compose_project="awf_ws_legacy",
        compose_file=Path("/workspaces/ws_legacy/compose.yml"),
        workspace_id="ws_legacy",
        clarification_model_services=("ollama-sidecar",),
    )

    assert result.ok
    runner.queue_result()
    runner.queue_result()

    rollback_result = await _rollback_persisted_clarification_model_network(
        runner, attachment=attachment
    )

    assert rollback_result.ok
    assert [call.args for call in runner.calls[-2:]] == [
        ["docker", "network", "disconnect", network_name, container_id],
        [
            "docker",
            "network",
            "connect",
            "--alias",
            "legacy-primary",
            "--alias",
            "legacy-secondary",
            network_name,
            container_id,
        ],
    ]
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


@pytest.mark.unit
async def test_rollback_persisted_model_network_continues_after_cleanup_failure() -> None:
    """A failed cleanup action does not strand later endpoints or the network."""
    network_name = "awf-ws_legacy-clarification-model-net"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="first endpoint remains attached")
    runner.queue_result()
    runner.queue_result()
    runner.queue_result()
    attachment = PersistedClarificationModelNetworkAttachment(
        network_name=network_name,
        created_network=True,
        connected_container_ids=["first-model-container", "second-model-container"],
        reconnecting_endpoints=[("existing-model-container", "ollama-sidecar")],
    )

    result = await _rollback_persisted_clarification_model_network(runner, attachment=attachment)

    assert result.stderr == "first endpoint remains attached"
    assert [call.args for call in runner.calls] == [
        ["docker", "network", "disconnect", network_name, "second-model-container"],
        ["docker", "network", "disconnect", network_name, "first-model-container"],
        [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            network_name,
            "existing-model-container",
        ],
        ["docker", "network", "rm", network_name],
    ]
    _assert_bounded_migration_calls(runner)


@pytest.mark.unit
async def test_attach_persisted_model_network_returns_failed_network_creation() -> None:
    """A failed network create leaves ownership unconfirmed for rollback."""
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=1,
        stderr="Error response from daemon: network awf-ws_legacy-clarification-model-net not found",
    )
    runner.queue_result(returncode=1, stderr="network create denied")

    attachment, result = await _attach_persisted_clarification_model_network(
        runner,
        compose_project="awf_ws_legacy",
        compose_file=Path("/workspaces/ws_legacy/compose.yml"),
        workspace_id="ws_legacy",
        clarification_model_services=("ollama-sidecar",),
    )

    assert attachment.created_network is False
    assert attachment.pending_network_creation_marker is not None
    assert result.stderr == "network create denied"


@pytest.mark.unit
async def test_rollback_persisted_model_network_preserves_concurrent_creator_network() -> None:
    """A losing create race does not remove the concurrent creator's network."""
    network_name = "awf-ws_legacy-clarification-model-net"
    runner = FakeCommandRunner()
    runner.queue_result(stdout="concurrent-attempt\\n")
    attachment = PersistedClarificationModelNetworkAttachment(
        network_name=network_name,
        pending_network_creation_marker="this-attempt",
    )

    result = await _rollback_persisted_clarification_model_network(runner, attachment=attachment)

    assert result.ok
    assert [call.args for call in runner.calls] == [
        [
            "docker",
            "network",
            "inspect",
            "--format",
            '{{ with .Labels }}{{ index . "io.awf.clarification-network-creation" }}{{ end }}',
            network_name,
        ]
    ]


@pytest.mark.unit
async def test_rollback_persisted_model_network_preserves_unlabeled_network() -> None:
    """An unlabeled race winner is not a cleanup failure or removal target."""
    network_name = "awf-ws_legacy-clarification-model-net"

    class _UnlabeledNetworkRunner:
        async def run(self, args: list[str], **_kwargs: object):  # type: ignore[no-untyped-def]
            if args[4] == '{{ index .Labels "io.awf.clarification-network-creation" }}':
                return CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="template: :1:3: executing at <.Labels>: nil data; no entry for key",
                )
            assert args == [
                "docker",
                "network",
                "inspect",
                "--format",
                '{{ with .Labels }}{{ index . "io.awf.clarification-network-creation" }}{{ end }}',
                network_name,
            ]
            return CommandResult(returncode=0, stdout="", stderr="")

    result = await _rollback_persisted_clarification_model_network(
        _UnlabeledNetworkRunner(),  # type: ignore[arg-type]
        attachment=PersistedClarificationModelNetworkAttachment(
            network_name=network_name,
            pending_network_creation_marker="this-attempt",
        ),
    )

    assert result.ok


@pytest.mark.unit
async def test_attach_persisted_model_network_returns_sidecar_discovery_failure() -> None:
    """A legacy re-ask does not continue when its model sidecar cannot be resolved."""
    runner = FakeCommandRunner()
    runner.queue_result()
    runner.queue_result(returncode=1, stderr="compose ps unavailable")

    _attachment, result = await _attach_persisted_clarification_model_network(
        runner,
        compose_project="awf_ws_legacy",
        compose_file=Path("/workspaces/ws_legacy/compose.yml"),
        workspace_id="ws_legacy",
        clarification_model_services=("ollama-sidecar",),
    )

    assert result.stderr == "compose ps unavailable"


@pytest.mark.unit
async def test_attach_persisted_model_network_preserves_reconnect_failure() -> None:
    """An endpoint that cannot be detached is reported instead of silently retried."""
    runner = FakeCommandRunner()
    runner.queue_result()
    runner.queue_result(stdout="stateful-model-container\n")
    runner.queue_result(
        returncode=1,
        stderr="endpoint with name stateful-model-container already exists in network",
    )
    runner.queue_result()
    runner.queue_result(returncode=1, stderr="network disconnect denied")

    attachment, result = await _attach_persisted_clarification_model_network(
        runner,
        compose_project="awf_ws_legacy",
        compose_file=Path("/workspaces/ws_legacy/compose.yml"),
        workspace_id="ws_legacy",
        clarification_model_services=("ollama-sidecar",),
    )

    assert attachment.reconnecting_endpoints == []
    assert [
        (endpoint.container_id, endpoint.aliases, endpoint.needs_disconnect)
        for endpoint in attachment.repaired_endpoints
    ] == [("stateful-model-container", (), False)]
    assert result.stderr == "network disconnect denied"


@pytest.mark.unit
async def test_attach_persisted_model_network_preserves_endpoint_when_alias_inspection_fails() -> (
    None
):
    """A failed alias check must not trigger a disruptive repair attempt."""
    runner = FakeCommandRunner()
    runner.queue_result(stdout="stateful-model-container\n")
    runner.queue_result(stdout="stateful-model-container\n")
    runner.queue_result(returncode=1, stderr="endpoint already connected")
    runner.queue_result(returncode=1, stderr="container inspect denied")

    _attachment, result = await _attach_persisted_clarification_model_network(
        runner,
        compose_project="awf_ws_legacy",
        compose_file=Path("/workspaces/ws_legacy/compose.yml"),
        workspace_id="ws_legacy",
        clarification_model_services=("ollama-sidecar",),
    )

    assert result.stderr == "container inspect denied"
    assert not any(call.args[2] == "disconnect" for call in runner.calls if len(call.args) > 2)


@pytest.mark.unit
async def test_attach_persisted_model_network_recognizes_short_preexisting_container_id() -> None:
    """A short Compose ID still identifies the full ID from network inspection."""
    full_container_id = "a" * 64
    short_container_id = full_container_id[:12]
    runner = FakeCommandRunner()
    runner.queue_result(stdout=f"{full_container_id}\n")
    runner.queue_result(stdout=f"{short_container_id}\n")
    runner.queue_result(returncode=1, stderr="network connect denied")
    runner.queue_result()

    attachment, result = await _attach_persisted_clarification_model_network(
        runner,
        compose_project="awf_ws_legacy",
        compose_file=Path("/workspaces/ws_legacy/compose.yml"),
        workspace_id="ws_legacy",
        clarification_model_services=("ollama-sidecar",),
    )

    assert attachment.connected_container_ids == []
    assert attachment.reconnecting_endpoints == [(short_container_id, "ollama-sidecar")]
    assert result.stderr == "network connect denied"

    rollback_result = await _rollback_persisted_clarification_model_network(
        runner, attachment=attachment
    )

    assert rollback_result.ok
    assert runner.calls[-1].args == [
        "docker",
        "network",
        "connect",
        "--alias",
        "ollama-sidecar",
        "awf-ws_legacy-clarification-model-net",
        short_container_id,
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attachment", "operation"),
    [
        pytest.param(
            PersistedClarificationModelNetworkAttachment(
                network_name="awf-ws_legacy-clarification-model-net",
                connected_container_ids=["model-container"],
            ),
            "disconnect",
            id="disconnect",
        ),
        pytest.param(
            PersistedClarificationModelNetworkAttachment(
                network_name="awf-ws_legacy-clarification-model-net",
                reconnecting_endpoints=[("model-container", "ollama-sidecar")],
            ),
            "connect",
            id="reconnect",
        ),
        pytest.param(
            PersistedClarificationModelNetworkAttachment(
                network_name="awf-ws_legacy-clarification-model-net",
                created_network=True,
            ),
            "rm",
            id="remove-network",
        ),
        pytest.param(
            PersistedClarificationModelNetworkAttachment(
                network_name="awf-ws_legacy-clarification-model-net",
                repaired_endpoints=[
                    _RepairedClarificationModelNetworkEndpoint(
                        container_id="model-container",
                        aliases=("legacy-alias",),
                        needs_disconnect=True,
                    )
                ],
            ),
            "disconnect",
            id="restore-disconnect",
        ),
        pytest.param(
            PersistedClarificationModelNetworkAttachment(
                network_name="awf-ws_legacy-clarification-model-net",
                repaired_endpoints=[
                    _RepairedClarificationModelNetworkEndpoint(
                        container_id="model-container",
                        aliases=("legacy-alias",),
                    )
                ],
            ),
            "connect",
            id="restore-connect",
        ),
    ],
)
async def test_rollback_persisted_model_network_reports_runner_exceptions(
    attachment: PersistedClarificationModelNetworkAttachment,
    operation: str,
) -> None:
    """Unexpected Docker client errors are returned as durable rollback failures."""

    class _RaisingRunner:
        async def run(self, args: list[str], **_kwargs: object):  # type: ignore[no-untyped-def]
            if operation in args:
                raise RuntimeError(f"{operation} failed")
            return CommandResult(returncode=0, stdout="", stderr="")

    result = await _rollback_persisted_clarification_model_network(
        _RaisingRunner(),  # type: ignore[arg-type]
        attachment=attachment,
    )

    assert result.returncode == 1
    assert result.stderr == f"RuntimeError: {operation} failed"


@pytest.mark.unit
async def test_rollback_persisted_model_network_treats_absent_network_as_complete() -> None:
    """A concurrent Docker network removal is an idempotent rollback success."""
    network_name = "awf-ws_legacy-clarification-model-net"
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=1,
        stdout="network was already removed",
        stderr=f"Error response from daemon: network {network_name} not found",
    )

    result = await _rollback_persisted_clarification_model_network(
        runner,
        attachment=PersistedClarificationModelNetworkAttachment(
            network_name=network_name,
            created_network=True,
        ),
    )

    assert result.ok
    assert result.stdout == "network was already removed"
