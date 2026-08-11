"""Compose-file migration helpers for isolated clarification runs."""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from awf.common.commands import AsyncCommandRunner, CommandResult


def _restore_compose_file(*, compose_file: Path, contents: bytes) -> None:
    """Atomically restore a Compose file after its sidecar update fails."""
    temporary_path: Path | None = None
    try:
        file_mode = compose_file.stat().st_mode & 0o777
        fd, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{compose_file.name}.", suffix=".tmp", dir=compose_file.parent
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(contents)
        temporary_path.chmod(file_mode)
        temporary_path.replace(compose_file)
    finally:
        if temporary_path is not None:
            with contextlib.suppress(OSError):
                temporary_path.unlink()


@dataclass
class PersistedClarificationModelNetworkAttachment:
    """Runtime network state added while upgrading a legacy clarification run."""

    network_name: str
    created_network: bool = False
    connected_container_ids: list[str] = field(default_factory=list)
    reconnecting_endpoints: list[tuple[str, str]] = field(default_factory=list)


def _clarification_model_network_name(*, compose_project: str, workspace_id: str | None) -> str:
    """Return the stable runtime name for a legacy clarification model network."""
    return f"awf-{workspace_id or compose_project.removeprefix('awf_')}-clarification-model-net"


def _network_is_absent(result: CommandResult, network_name: str) -> bool:
    """Return whether Docker reported that a model network has already gone away."""
    return not result.ok and result.stderr.rstrip().endswith(f"network {network_name} not found")


def _network_is_already_connected(result: CommandResult) -> bool:
    """Return whether an idempotent network connect found its existing endpoint."""
    detail = f"{result.stdout}\n{result.stderr}".lower()
    return "already connected" in detail or "endpoint with name" in detail and "exists" in detail


def _network_is_not_connected(result: CommandResult) -> bool:
    """Return whether an idempotent disconnect found no endpoint to remove."""
    detail = f"{result.stdout}\n{result.stderr}".lower()
    return "not connected" in detail or "no such endpoint" in detail


async def _attach_persisted_clarification_model_network(
    runner: AsyncCommandRunner,
    *,
    compose_project: str,
    compose_file: Path,
    workspace_id: str | None,
    clarification_model_services: tuple[str, ...],
    attachment: PersistedClarificationModelNetworkAttachment | None = None,
) -> tuple[PersistedClarificationModelNetworkAttachment, CommandResult]:
    """Connect live legacy model sidecars to clarification without recreating them."""
    if attachment is None:
        attachment = PersistedClarificationModelNetworkAttachment(
            network_name=_clarification_model_network_name(
                compose_project=compose_project, workspace_id=workspace_id
            )
        )
    network_inspect_result = await runner.run(
        ["docker", "network", "inspect", attachment.network_name]
    )
    if not network_inspect_result.ok:
        if not _network_is_absent(network_inspect_result, attachment.network_name):
            return attachment, network_inspect_result
        network_create_result = await runner.run(
            [
                "docker",
                "network",
                "create",
                "--internal",
                "--label",
                f"com.docker.compose.project={compose_project}",
                "--label",
                "com.docker.compose.network=clarification_model_net",
                attachment.network_name,
            ]
        )
        if not network_create_result.ok:
            return attachment, network_create_result
        attachment.created_network = True
    for service in clarification_model_services:
        container_ids_result = await runner.run(
            [
                "docker",
                "compose",
                "-p",
                compose_project,
                "-f",
                str(compose_file),
                "ps",
                "-q",
                service,
            ]
        )
        if not container_ids_result.ok:
            return attachment, container_ids_result
        container_ids = tuple(
            dict.fromkeys(
                container_id.strip() for container_id in container_ids_result.stdout.splitlines()
            )
        )
        if not container_ids:
            return attachment, CommandResult(
                returncode=1,
                stdout="",
                stderr=f"no running container found for clarification model service {service}",
            )
        for container_id in container_ids:
            # Record before awaiting: cancellation can arrive after Docker
            # connects the endpoint but before its subprocess result reaches
            # us, and rollback must still detach that possible connection.
            attachment.connected_container_ids.append(container_id)
            network_connect_result = await runner.run(
                [
                    "docker",
                    "network",
                    "connect",
                    "--alias",
                    service,
                    attachment.network_name,
                    container_id,
                ]
            )
            if network_connect_result.ok:
                continue
            if _network_is_already_connected(network_connect_result):
                attachment.connected_container_ids.pop()
                attachment.reconnecting_endpoints.append((container_id, service))
                network_disconnect_result = await runner.run(
                    ["docker", "network", "disconnect", attachment.network_name, container_id]
                )
                if not network_disconnect_result.ok and not _network_is_not_connected(
                    network_disconnect_result
                ):
                    return attachment, network_disconnect_result
                network_connect_result = await runner.run(
                    [
                        "docker",
                        "network",
                        "connect",
                        "--alias",
                        service,
                        attachment.network_name,
                        container_id,
                    ]
                )
                if network_connect_result.ok:
                    attachment.reconnecting_endpoints.pop()
                    continue
                return attachment, network_connect_result
            return attachment, network_connect_result
    return attachment, CommandResult(returncode=0, stdout="", stderr="")


async def _rollback_persisted_clarification_model_network(
    runner: AsyncCommandRunner,
    *,
    attachment: PersistedClarificationModelNetworkAttachment,
) -> CommandResult:
    """Undo only the live network attachments made by a failed legacy re-ask."""
    for container_id in reversed(attachment.connected_container_ids):
        try:
            network_disconnect_result = await runner.run(
                ["docker", "network", "disconnect", attachment.network_name, container_id]
            )
        except Exception as exc:
            return CommandResult(returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}")
        if (
            not network_disconnect_result.ok
            and not _network_is_absent(network_disconnect_result, attachment.network_name)
            and not _network_is_not_connected(network_disconnect_result)
        ):
            return network_disconnect_result
    for container_id, service in reversed(attachment.reconnecting_endpoints):
        try:
            network_connect_result = await runner.run(
                [
                    "docker",
                    "network",
                    "connect",
                    "--alias",
                    service,
                    attachment.network_name,
                    container_id,
                ]
            )
        except Exception as exc:
            return CommandResult(returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}")
        if not network_connect_result.ok and not _network_is_already_connected(
            network_connect_result
        ):
            return network_connect_result
    if not attachment.created_network:
        return CommandResult(returncode=0, stdout="", stderr="")
    try:
        network_remove_result = await runner.run(
            ["docker", "network", "rm", attachment.network_name]
        )
    except Exception as exc:
        return CommandResult(returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}")
    if _network_is_absent(network_remove_result, attachment.network_name):
        return CommandResult(returncode=0, stdout=network_remove_result.stdout, stderr="")
    return network_remove_result
