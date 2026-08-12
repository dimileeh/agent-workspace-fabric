"""Compose-file migration helpers for isolated clarification runs."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from uuid import uuid4

from awf.common.commands import AsyncCommandRunner, CommandResult

PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS: Final[float] = 30.0
"""Maximum time for each Docker command in the persisted-network migration."""

_NETWORK_CREATION_MARKER_LABEL: Final[str] = "io.awf.clarification-network-creation"
_NETWORK_CREATION_MARKER_FORMAT: Final[str] = (
    f'{{{{ with .Labels }}}}{{{{ index . "{_NETWORK_CREATION_MARKER_LABEL}" }}}}{{{{ end }}}}'
)
_NETWORK_CONNECTED_CONTAINER_IDS_FORMAT: Final[str] = (
    r'{{ range $container_id, $_ := .Containers }}{{ printf "%s\n" $container_id }}{{ end }}'
)


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
class _RepairedClarificationModelNetworkEndpoint:
    """Original aliases and replacement progress for one repaired endpoint."""

    container_id: str
    aliases: tuple[str, ...]
    needs_disconnect: bool = False


@dataclass
class PersistedClarificationModelNetworkAttachment:
    """Runtime network state added while upgrading a legacy clarification run."""

    network_name: str
    created_network: bool = False
    pending_network_creation_marker: str | None = None
    connected_container_ids: list[str] = field(default_factory=list)
    reconnecting_endpoints: list[tuple[str, str]] = field(default_factory=list)
    repaired_endpoints: list[_RepairedClarificationModelNetworkEndpoint] = field(
        default_factory=list
    )


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


def _container_network_aliases_format(network_name: str) -> str:
    """Return Docker's template for aliases assigned on one selected network."""
    return (
        f"{{{{ with index .NetworkSettings.Networks {json.dumps(network_name)} }}}}"
        r'{{ range .Aliases }}{{ printf "%s\n" . }}{{ end }}'
        r"{{ end }}"
    )


def _container_id_was_preexisting(
    container_id: str, preexisting_container_ids: frozenset[str]
) -> bool:
    """Return whether a possibly abbreviated Compose ID was already on the network."""
    return any(
        preexisting_container_id.startswith(container_id)
        for preexisting_container_id in preexisting_container_ids
    )


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
        [
            "docker",
            "network",
            "inspect",
            "--format",
            _NETWORK_CONNECTED_CONTAINER_IDS_FORMAT,
            attachment.network_name,
        ],
        timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
    )
    preexisting_container_ids = frozenset(
        container_id.strip()
        for container_id in network_inspect_result.stdout.splitlines()
        if container_id.strip()
    )
    if not network_inspect_result.ok:
        if not _network_is_absent(network_inspect_result, attachment.network_name):
            return attachment, network_inspect_result
        # A cancellation can arrive after Docker creates the network but before
        # its subprocess result reaches us. Keep a per-attempt marker so
        # rollback can confirm ownership without removing a concurrent
        # clarification run's network.
        attachment.pending_network_creation_marker = uuid4().hex
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
                "--label",
                (f"{_NETWORK_CREATION_MARKER_LABEL}={attachment.pending_network_creation_marker}"),
                attachment.network_name,
            ],
            timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
        )
        if not network_create_result.ok:
            return attachment, network_create_result
        attachment.created_network = True
        attachment.pending_network_creation_marker = None
        preexisting_container_ids = frozenset()
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
            ],
            timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
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
            # us. Preserve endpoints that predate this attempt, while still
            # detaching endpoints added by this attempt during rollback.
            endpoint_preexisted = _container_id_was_preexisting(
                container_id, preexisting_container_ids
            )
            if endpoint_preexisted:
                attachment.reconnecting_endpoints.append((container_id, service))
            else:
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
                ],
                timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
            )
            if network_connect_result.ok:
                continue
            if _network_is_already_connected(network_connect_result):
                if not endpoint_preexisted:
                    attachment.connected_container_ids.pop()
                    attachment.reconnecting_endpoints.append((container_id, service))
                alias_inspect_result = await runner.run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        _container_network_aliases_format(attachment.network_name),
                        container_id,
                    ],
                    timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
                )
                if not alias_inspect_result.ok:
                    return attachment, alias_inspect_result
                aliases = tuple(
                    alias for alias in alias_inspect_result.stdout.splitlines() if alias
                )
                if service in aliases:
                    attachment.reconnecting_endpoints.pop()
                    continue
                attachment.reconnecting_endpoints.pop()
                repaired_endpoint = _RepairedClarificationModelNetworkEndpoint(
                    container_id=container_id, aliases=aliases
                )
                attachment.repaired_endpoints.append(repaired_endpoint)
                network_disconnect_result = await runner.run(
                    ["docker", "network", "disconnect", attachment.network_name, container_id],
                    timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
                )
                if not network_disconnect_result.ok and not _network_is_not_connected(
                    network_disconnect_result
                ):
                    return attachment, network_disconnect_result
                repaired_endpoint.needs_disconnect = True
                network_connect_result = await runner.run(
                    [
                        "docker",
                        "network",
                        "connect",
                        "--alias",
                        service,
                        attachment.network_name,
                        container_id,
                    ],
                    timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
                )
                if network_connect_result.ok:
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
    first_failure: CommandResult | None = None
    for container_id in reversed(attachment.connected_container_ids):
        try:
            network_disconnect_result = await runner.run(
                ["docker", "network", "disconnect", attachment.network_name, container_id],
                timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            network_disconnect_result = CommandResult(
                returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}"
            )
        if first_failure is None and (
            not network_disconnect_result.ok
            and not _network_is_absent(network_disconnect_result, attachment.network_name)
            and not _network_is_not_connected(network_disconnect_result)
        ):
            first_failure = network_disconnect_result
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
                ],
                timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            network_connect_result = CommandResult(
                returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}"
            )
        if (
            first_failure is None
            and not network_connect_result.ok
            and not _network_is_already_connected(network_connect_result)
        ):
            first_failure = network_connect_result
    for repaired_endpoint in reversed(attachment.repaired_endpoints):
        if repaired_endpoint.needs_disconnect:
            try:
                network_disconnect_result = await runner.run(
                    [
                        "docker",
                        "network",
                        "disconnect",
                        attachment.network_name,
                        repaired_endpoint.container_id,
                    ],
                    timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                network_disconnect_result = CommandResult(
                    returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}"
                )
            if first_failure is None and (
                not network_disconnect_result.ok
                and not _network_is_absent(network_disconnect_result, attachment.network_name)
                and not _network_is_not_connected(network_disconnect_result)
            ):
                first_failure = network_disconnect_result
        try:
            network_connect_result = await runner.run(
                [
                    "docker",
                    "network",
                    "connect",
                    *[
                        argument
                        for alias in repaired_endpoint.aliases
                        for argument in ("--alias", alias)
                    ],
                    attachment.network_name,
                    repaired_endpoint.container_id,
                ],
                timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            network_connect_result = CommandResult(
                returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}"
            )
        if (
            first_failure is None
            and not network_connect_result.ok
            and not _network_is_already_connected(network_connect_result)
        ):
            first_failure = network_connect_result
    if not attachment.created_network and attachment.pending_network_creation_marker is None:
        return first_failure or CommandResult(returncode=0, stdout="", stderr="")
    if not attachment.created_network:
        try:
            network_marker_result = await runner.run(
                [
                    "docker",
                    "network",
                    "inspect",
                    "--format",
                    _NETWORK_CREATION_MARKER_FORMAT,
                    attachment.network_name,
                ],
                timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            network_marker_result = CommandResult(
                returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}"
            )
        if not network_marker_result.ok:
            if _network_is_absent(network_marker_result, attachment.network_name):
                return first_failure or CommandResult(
                    returncode=0, stdout=network_marker_result.stdout, stderr=""
                )
            return first_failure or network_marker_result
        if network_marker_result.stdout.strip() != attachment.pending_network_creation_marker:
            return first_failure or CommandResult(returncode=0, stdout="", stderr="")
    try:
        network_remove_result = await runner.run(
            ["docker", "network", "rm", attachment.network_name],
            timeout_seconds=PERSISTED_CLARIFICATION_MODEL_NETWORK_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        network_remove_result = CommandResult(
            returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}"
        )
    if _network_is_absent(network_remove_result, attachment.network_name):
        network_remove_result = CommandResult(
            returncode=0, stdout=network_remove_result.stdout, stderr=""
        )
    return first_failure or network_remove_result
