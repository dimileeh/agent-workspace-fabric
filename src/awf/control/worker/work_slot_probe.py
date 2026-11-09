"""Local process probe for work-slot leases (slot-release).

Answers "is an ``awf-exec`` tagged with this work-slot invocation still running
in the workspace's agent container?" for the worker's startup check and periodic
reconciler. Only proof that the process is gone may release a lease: a runtime
that cannot be queried at all is ``unavailable`` (the worker fails closed
node-wide), and a lease that cannot be proven either way is ``unknown`` (that
lease alone is kept).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from awf.common.commands import AsyncCommandRunner
from awf.common.compose_exec import (
    WORK_SLOT_PROBE_TIMEOUT_SECONDS,
    probe_work_slot_invocation,
)
from awf.control.worker.logging import _log
from awf.control.worker.protocols import RuntimeInspectorProtocol, WorkSlotLeaseProbeResult

_AGENT_SERVICE = "agent"


class LocalWorkSlotProcessProbe:
    """Probe the workspace agent container through ``docker compose exec``."""

    def __init__(
        self,
        *,
        runner: AsyncCommandRunner,
        runtime_inspector: RuntimeInspectorProtocol,
        inspect_timeout_seconds: float = WORK_SLOT_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner
        self._runtime_inspector = runtime_inspector
        self._inspect_timeout_seconds = inspect_timeout_seconds

    async def probe(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        compose_project: str | None,
        compose_file: Path | None,
    ) -> WorkSlotLeaseProbeResult:
        # Unset compose metadata is not proof of no stack: the executor runs
        # tagged execs in this default project when the column is empty.
        compose_project = compose_project or f"awf_{workspace_id}"
        try:
            # The worker loop awaits this inline: a wedged Docker CLI must not stall it.
            snapshot = await asyncio.wait_for(
                self._runtime_inspector.inspect(compose_project),
                timeout=self._inspect_timeout_seconds,
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            _log.warning(
                "worker.work_slot_probe_inspect_failed",
                workspace_id=workspace_id,
                invocation_id=invocation_id,
                error_type=type(exc).__name__,
            )
            return "unavailable"
        if snapshot.stack_state == "unavailable":
            # ``docker ps`` itself failed: the daemon cannot be queried.
            return "unavailable"
        agent = next(
            (
                service
                for service in snapshot.services
                if service.name == _AGENT_SERVICE and not service.one_off
            ),
            None,
        )
        if agent is None and any(
            not service.service_label_available and service.state.strip().lower() == "running"
            for service in snapshot.services
        ):
            # A failed ``docker inspect`` hides the Compose service label, so a
            # running unlabeled container may still be the agent: fail closed.
            return "unknown"
        if agent is None or agent.state.strip().lower() != "running":
            # No running agent container means no tagged process can survive.
            return "absent"
        if compose_file is None:
            # A running agent we cannot exec into cannot be proven idle.
            return "unknown"
        try:
            return await probe_work_slot_invocation(
                self._runner,
                compose_project=compose_project,
                compose_file=compose_file,
                invocation_id=invocation_id,
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            _log.warning(
                "worker.work_slot_probe_exec_failed",
                workspace_id=workspace_id,
                invocation_id=invocation_id,
                error_type=type(exc).__name__,
            )
            return "unknown"
