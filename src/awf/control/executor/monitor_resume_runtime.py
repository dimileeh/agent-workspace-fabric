"""Monitor resume runtime restart that preserves recovering dependencies (#936).

A monitor resume (restart recovery, ``remonitor``, ``guide``) used to
force-recreate the whole compose project. That restarted dependencies still in
their own crash recovery, such as Postgres replaying its WAL after an unclean
stop, so the resume could fail and be retried forever. The restart now:

1. starts each exited or absent dependency on its own with ``--no-recreate --no-deps`` and
   no ``--wait``, so no compose health gate can fail it on the healthcheck
   retries stored in a container created from an older compose.yml (the normal
   state after a host restart, as every service runs with ``restart: "no"``);
2. waits for every running declared dependency (never a ``compose run`` or
   orphan container) whose Docker health is ``starting`` or
   ``unhealthy`` to become healthy, bounded by the stack's startup budget rather
   than by those rendered retries (Docker keeps probing an ``unhealthy``
   container and flips it back on success);
3. brings the dependencies up with ``--no-recreate``, which starts exited or
   absent containers and never recreates an existing one; the agent is left out
   so its stale container is neither started nor waited on before step 4;
4. recreates only the agent with ``--force-recreate --no-deps agent``: auth
   overlay bind mounts are bound at container creation, and #1090's setup
   replay keys off the agent's new identity. A failure here raises
   ``AgentRecreateError``: unless the agent identity verifiably changed, the
   caller counts it as a failed restart, never as a usable runtime.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

from awf.common.audit import redact_audit_text
from awf.control.executor.constants import MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT_REASON_CODE
from awf.control.executor.quality_gates import _log
from awf.node.compose_manager import ComposeOperationError
from awf.runtime.inspection import RuntimeInspector, RuntimeService, RuntimeSnapshot

AGENT_SERVICE_NAME: Final[str] = "agent"
DEPENDENCY_POLL_INTERVAL_SECONDS: Final[float] = 5.0
DEPENDENCY_INSPECT_TIMEOUT_SECONDS: Final[float] = 30.0
"""Upper bound on one ``docker ps``/``docker inspect`` round of the dependency wait."""
_RECOVERING_HEALTH: Final = frozenset({"starting", "unhealthy"})
_STOPPED_STATES: Final = frozenset({"exited", "created"})
_UNREADABLE_STACK_STATES: Final = frozenset({"unknown", "unavailable"})
_sleep = asyncio.sleep
_monotonic = time.monotonic


class DependencyWaitTimeoutError(ComposeOperationError):
    """The dependency wait ran out of budget; names each dependency still unsettled."""

    def __init__(self, *, budget_seconds: int, recovering: dict[str, str]) -> None:
        detail = ", ".join(f"{name}={health}" for name, health in sorted(recovering.items()))
        super().__init__(
            operation="dependency_wait",
            returncode=1,
            stdout="",
            stderr=(
                f"dependencies still recovering after {budget_seconds}s "
                f"(docker.startup_timeout_seconds budget): {detail}"
            ),
            reason_code=MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT_REASON_CODE,
        )
        self.unsettled_services = tuple(sorted(recovering))


class AgentRecreateError(ComposeOperationError):
    """The final agent force-recreate failed, so only the stale agent is left running.

    Unless the agent identity changed anyway, the old agent has neither the
    refreshed auth overlays and env nor a new identity for #1090's setup
    replay, so the caller counts it as a failed restart however healthy the
    old stack looks.
    """

    def __init__(self, error: ComposeOperationError) -> None:
        super().__init__(
            operation=error.operation,
            returncode=error.returncode,
            stdout=error.stdout,
            stderr=error.stderr,
            reason_code=error.reason_code,
        )


async def inspect_resume_runtime(compose_project: str) -> RuntimeSnapshot:
    """Inspect the compose project under a timeout so a wedged daemon cannot stall resume."""
    return await asyncio.wait_for(
        RuntimeInspector().inspect(compose_project),
        timeout=DEPENDENCY_INSPECT_TIMEOUT_SECONDS,
    )


def _dependency_containers(
    snapshot: RuntimeSnapshot,
    declared_services: tuple[str, ...] | None,
    recreate_services: tuple[str, ...] = (),
) -> list[RuntimeService]:
    """Return the containers of declared non-agent services that are not recreated.

    ``compose run`` containers and orphans of services no longer in compose.yml
    never settle on their own; ``declared_services`` is ``None`` when compose.yml
    is unreadable, and then every non-agent service counts. Services the resume
    force-recreates are not worth waiting on or starting first.
    """
    return [
        service
        for service in snapshot.services
        if service.name != AGENT_SERVICE_NAME
        and not service.one_off
        and (declared_services is None or service.name in declared_services)
        and service.name not in recreate_services
    ]


def _recovering_dependencies(
    snapshot: RuntimeSnapshot,
    declared_services: tuple[str, ...] | None,
    recreate_services: tuple[str, ...] = (),
) -> dict[str, str]:
    """Return running dependencies whose health is not settled yet."""
    recovering: dict[str, str] = {}
    for service in _dependency_containers(snapshot, declared_services, recreate_services):
        if service.state.strip().lower() != "running":
            continue
        health = (service.health or "").strip().lower()
        if health in _RECOVERING_HEALTH:
            recovering[service.name] = health
    return recovering


def _stopped_dependencies(
    snapshot: RuntimeSnapshot,
    declared_services: tuple[str, ...] | None,
    recreate_services: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return dependencies whose container is not running or, when declared, absent.

    An absent container (a released runtime) would otherwise be created by the
    ``up --wait`` and gated on the healthcheck retries of an older compose.yml.
    """
    containers = _dependency_containers(snapshot, declared_services, recreate_services)
    stopped = {
        service.name for service in containers if service.state.strip().lower() in _STOPPED_STATES
    }
    if declared_services is not None:
        present = {service.name for service in containers}
        stopped.update(
            name
            for name in declared_services
            if name != AGENT_SERVICE_NAME and name not in recreate_services and name not in present
        )
    return tuple(sorted(stopped))


async def wait_for_recovering_dependencies(
    compose_project: str,
    *,
    workspace_id: str,
    budget_seconds: int,
    start_stopped: Callable[[tuple[str, ...]], Awaitable[None]] | None = None,
    declared_services: tuple[str, ...] | None = None,
    recreate_services: tuple[str, ...] = (),
) -> None:
    """Wait until no running dependency is ``starting``/``unhealthy``, within the budget.

    ``start_stopped`` receives the dependencies stopped in the first snapshot; once
    it has started them they are waited on like any other running dependency.

    Raises ``DependencyWaitTimeoutError`` (``MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT``)
    naming each unsettled service when the budget runs out. An unreadable runtime
    is logged and skipped: the compose call that follows surfaces the real error.
    """
    deadline = _monotonic() + budget_seconds
    start_pending = start_stopped
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
        if start_pending is not None:
            start, start_pending = start_pending, None
            stopped = _stopped_dependencies(snapshot, declared_services, recreate_services)
            if stopped:
                await start(stopped)
                continue
        recovering = _recovering_dependencies(snapshot, declared_services, recreate_services)
        if not recovering:
            return
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise DependencyWaitTimeoutError(budget_seconds=budget_seconds, recovering=recovering)
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
    dependency_services: tuple[str, ...] | None,
    runtime_usable: Callable[[], Awaitable[bool]] | None = None,
    recreate_services: tuple[str, ...] = (),
) -> ComposeOperationError | None:
    """Restart the runtime for a monitor resume, recreating only what must change.

    ``dependency_services`` lists the compose services that are not the agent;
    ``None`` (unreadable compose file) falls back to bringing up the whole project.
    ``recreate_services`` are dependencies force-recreated with the agent (env
    rewritten in compose.yml or backed by a set env secret, or still unsettled
    when the last attempt gave up);
    every other dependency is never recreated.
    When the dependency up fails, ``runtime_usable`` decides whether the agent is
    still recreated (auth overlays, env and #1090's setup replay depend on it);
    the tolerated failure is returned so the caller can record it. A failed
    agent recreate raises ``AgentRecreateError`` and is never tolerated.
    """
    common: dict[str, Any] = {
        "project_name": compose_project,
        "compose_file": compose_file,
        "workspace_id": workspace_id,
        "wait": True,
        "compose_up_timeout_seconds": budget_seconds,
    }

    async def _start_stopped(services: tuple[str, ...]) -> None:
        _log.info(
            "executor.resume_dependency_start",
            workspace_id=workspace_id,
            compose_project_name=compose_project,
            services=list(services),
        )
        # One service per call: ``--no-deps`` then skips every depends_on health gate.
        for service in services:
            await compose.ensure_project_up(
                **{**common, "wait": False},
                no_recreate=True,
                no_deps=True,
                services=(service,),
            )

    await wait_for_recovering_dependencies(
        compose_project,
        workspace_id=workspace_id,
        budget_seconds=budget_seconds,
        start_stopped=_start_stopped,
        declared_services=dependency_services,
        recreate_services=recreate_services,
    )
    tolerated: ComposeOperationError | None = None
    try:
        if dependency_services is None:
            await compose.ensure_project_up(**common, no_recreate=True)
        else:
            dependencies = tuple(
                name
                for name in dependency_services
                if name != AGENT_SERVICE_NAME and name not in recreate_services
            )
            if dependencies:
                await compose.ensure_project_up(**common, no_recreate=True, services=dependencies)
    except ComposeOperationError as exc:
        if runtime_usable is None or not await runtime_usable():
            raise
        _log.warning(
            "executor.resume_dependency_up_failed_runtime_still_usable",
            workspace_id=workspace_id,
            compose_project_name=compose_project,
            reason_code=exc.reason_code,
            stderr=redact_audit_text(exc.stderr, limit=1000),
        )
        tolerated = exc
    try:
        await compose.ensure_project_up(
            **common,
            force_recreate=True,
            no_deps=True,
            services=(
                AGENT_SERVICE_NAME,
                *sorted(set(recreate_services) - {AGENT_SERVICE_NAME}),
            ),
        )
    except ComposeOperationError as exc:
        raise AgentRecreateError(exc) from exc
    return tolerated
