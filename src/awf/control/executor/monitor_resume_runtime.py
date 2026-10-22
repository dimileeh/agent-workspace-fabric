"""Monitor resume runtime restart that preserves recovering dependencies (#936).

A monitor resume (restart recovery, ``remonitor``, ``guide``) used to
force-recreate the whole compose project. That restarted dependencies still in
their own crash recovery, such as Postgres replaying its WAL after an unclean
stop, so the resume could fail and be retried forever. The restart now:

1. waits for every running dependency whose Docker health is ``starting`` or
   ``unhealthy`` to become healthy, bounded by the stack's startup budget rather
   than by the healthcheck retries rendered into an existing compose.yml (Docker
   keeps probing an ``unhealthy`` container and flips it back on success);
2. brings the project up with ``--no-recreate``, which starts exited or absent
   containers and never recreates an existing one;
3. recreates only the agent with ``--force-recreate --no-deps agent``: auth
   overlay bind mounts are bound at container creation, and #1090's setup
   replay keys off the agent's new identity.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Final

from awf.common.audit import redact_audit_text
from awf.control.executor.constants import MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT_REASON_CODE
from awf.control.executor.quality_gates import _log
from awf.node.compose_manager import ComposeOperationError
from awf.runtime.inspection import RuntimeInspector, RuntimeSnapshot

AGENT_SERVICE_NAME: Final[str] = "agent"
DEPENDENCY_POLL_INTERVAL_SECONDS: Final[float] = 5.0
DEPENDENCY_INSPECT_TIMEOUT_SECONDS: Final[float] = 30.0
"""Upper bound on one ``docker ps``/``docker inspect`` round of the dependency wait."""
_RECOVERING_HEALTH: Final = frozenset({"starting", "unhealthy"})
_UNREADABLE_STACK_STATES: Final = frozenset({"unknown", "unavailable"})
_sleep = asyncio.sleep
_monotonic = time.monotonic


async def inspect_resume_runtime(compose_project: str) -> RuntimeSnapshot:
    """Inspect the compose project under a timeout so a wedged daemon cannot stall resume."""
    return await asyncio.wait_for(
        RuntimeInspector().inspect(compose_project),
        timeout=DEPENDENCY_INSPECT_TIMEOUT_SECONDS,
    )


def _recovering_dependencies(snapshot: RuntimeSnapshot) -> dict[str, str]:
    """Return running non-agent services whose health is not settled yet."""
    recovering: dict[str, str] = {}
    for service in snapshot.services:
        if service.name == AGENT_SERVICE_NAME:
            continue
        if service.state.strip().lower() != "running":
            continue
        health = (service.health or "").strip().lower()
        if health in _RECOVERING_HEALTH:
            recovering[service.name] = health
    return recovering


async def wait_for_recovering_dependencies(
    compose_project: str,
    *,
    workspace_id: str,
    budget_seconds: int,
) -> None:
    """Wait until no running dependency is ``starting``/``unhealthy``, within the budget.

    Raises ``ComposeOperationError`` with ``MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT``
    naming each unsettled service when the budget runs out. An unreadable runtime
    is logged and skipped: the compose call that follows surfaces the real error.
    """
    deadline = _monotonic() + budget_seconds
    while True:
        try:
            snapshot = await inspect_resume_runtime(compose_project)
        except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
            _log.warning(
                "executor.resume_dependency_wait_inspect_failed",
                workspace_id=workspace_id,
                compose_project_name=compose_project,
                error=redact_audit_text(repr(exc), limit=500),
            )
            return
        if snapshot.stack_state in _UNREADABLE_STACK_STATES:
            _log.warning(
                "executor.resume_dependency_wait_runtime_unreadable",
                workspace_id=workspace_id,
                compose_project_name=compose_project,
                stack_state=snapshot.stack_state,
            )
            return
        recovering = _recovering_dependencies(snapshot)
        if not recovering:
            return
        remaining = deadline - _monotonic()
        if remaining <= 0:
            detail = ", ".join(f"{name}={health}" for name, health in sorted(recovering.items()))
            raise ComposeOperationError(
                operation="dependency_wait",
                returncode=1,
                stdout="",
                stderr=(
                    f"dependencies still recovering after {budget_seconds}s "
                    f"(docker.startup_timeout_seconds budget): {detail}"
                ),
                reason_code=MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT_REASON_CODE,
            )
        _log.info(
            "executor.resume_dependency_wait",
            workspace_id=workspace_id,
            compose_project_name=compose_project,
            recovering=recovering,
            remaining_seconds=round(remaining, 1),
        )
        await _sleep(min(DEPENDENCY_POLL_INTERVAL_SECONDS, remaining))


async def restart_runtime_for_monitor_resume(
    compose: Any,
    *,
    compose_project: str,
    compose_file: Path,
    workspace_id: str,
    budget_seconds: int,
) -> None:
    """Restart the runtime for a monitor resume without recreating any dependency."""
    await wait_for_recovering_dependencies(
        compose_project,
        workspace_id=workspace_id,
        budget_seconds=budget_seconds,
    )
    common: dict[str, Any] = {
        "project_name": compose_project,
        "compose_file": compose_file,
        "workspace_id": workspace_id,
        "wait": True,
        "compose_up_timeout_seconds": budget_seconds,
    }
    await compose.ensure_project_up(**common, no_recreate=True)
    await compose.ensure_project_up(
        **common,
        force_recreate=True,
        no_deps=True,
        services=(AGENT_SERVICE_NAME,),
    )
