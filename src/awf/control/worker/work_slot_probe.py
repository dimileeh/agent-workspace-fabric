"""Local process probe for work-slot leases (slot-release).

Answers "is an ``awf-exec`` tagged with this work-slot invocation still running
in the workspace's agent container?" for the worker's startup check and periodic
reconciler. Docker/Compose inspection failures are ``unknown`` (fail closed),
never ``absent``: only proof that the process is gone may release a lease.
"""

from __future__ import annotations

from pathlib import Path

from awf.common.commands import AsyncCommandRunner
from awf.common.compose_exec import WorkSlotProbeResult, probe_work_slot_invocation
from awf.control.worker.logging import _log
from awf.control.worker.protocols import RuntimeInspectorProtocol

_AGENT_SERVICE = "agent"


class LocalWorkSlotProcessProbe:
    """Probe the workspace agent container through ``docker compose exec``."""

    def __init__(
        self,
        *,
        runner: AsyncCommandRunner,
        runtime_inspector: RuntimeInspectorProtocol,
    ) -> None:
        self._runner = runner
        self._runtime_inspector = runtime_inspector

    async def probe(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        compose_project: str | None,
        compose_file: Path | None,
    ) -> WorkSlotProbeResult:
        if not compose_project or compose_file is None:
            return "absent"
        try:
            snapshot = await self._runtime_inspector.inspect(compose_project)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            _log.warning(
                "worker.work_slot_probe_inspect_failed",
                workspace_id=workspace_id,
                invocation_id=invocation_id,
                error_type=type(exc).__name__,
            )
            return "unknown"
        if snapshot.stack_state == "unavailable":
            return "unknown"
        agent = next(
            (
                service
                for service in snapshot.services
                if service.name == _AGENT_SERVICE and not service.one_off
            ),
            None,
        )
        if agent is None or agent.state.strip().lower() != "running":
            # No running agent container means no tagged process can survive.
            return "absent"
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
