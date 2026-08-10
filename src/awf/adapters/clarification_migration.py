"""Compose-file migration helpers for isolated clarification runs."""

from __future__ import annotations

import contextlib
import os
import tempfile
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


async def _reap_persisted_clarification_model_migration(
    runner: AsyncCommandRunner,
    *,
    compose_project: str,
    compose_file: Path,
    workspace_id: str | None,
    clarification_model_services: tuple[str, ...],
) -> CommandResult:
    """Remove migrated sidecars and return the result of reaping their network."""
    with contextlib.suppress(Exception):
        await runner.run(
            [
                "docker",
                "compose",
                "-p",
                compose_project,
                "-f",
                str(compose_file),
                "rm",
                "--stop",
                "--force",
                *clarification_model_services,
            ]
        )
    network_name = (
        f"awf-{workspace_id or compose_project.removeprefix('awf_')}-clarification-model-net"
    )
    network_reap_result = await runner.run(
        [
            "docker",
            "network",
            "rm",
            network_name,
        ]
    )
    if not network_reap_result.ok and network_reap_result.stderr.rstrip().endswith(
        f"network {network_name} not found"
    ):
        # Another teardown can win the network-removal race. Docker reports
        # that idempotent state as an error, but the migration is reaped.
        return CommandResult(returncode=0, stdout=network_reap_result.stdout, stderr="")
    return network_reap_result
