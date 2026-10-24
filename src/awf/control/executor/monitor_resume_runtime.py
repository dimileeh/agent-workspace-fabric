"""Monitor resume runtime restart that preserves recovering dependencies (#936).

A monitor resume (restart recovery, ``remonitor``, ``guide``) used to
force-recreate the whole compose project. That restarted dependencies still in
their own crash recovery, such as Postgres replaying its WAL after an unclean
stop, so the resume could fail and be retried forever. The restart now:

1. force-recreates each dependency the resume must recreate (``recreate_services``),
   a dependent only once the prerequisites it depends on are satisfied (starting
   any exited or absent one first), and starts each other exited or absent
   dependency on its own with ``--no-recreate``, all ``--no-deps`` and without
   ``--wait``, so no compose health gate can fail them on the healthcheck
   retries stored in a container created from an older compose.yml (the normal
   state after a host restart, as every service runs with ``restart: "no"``).
   Nothing is waited on before every prerequisite is started or recreated, a
   failed prerequisite of a forced recreate fails the restart, and
   a stopped dependency starts only once its compose ``depends_on``
   prerequisites are satisfied (healthy, exited 0, or running, per condition);
   one whose prerequisite failed is left to step 3;
2. waits for every running declared dependency (never a ``compose run`` or
   orphan container) whose Docker health is ``starting`` or
   ``unhealthy`` to become healthy, bounded by the stack's startup budget rather
   than by those rendered retries (Docker keeps probing an ``unhealthy``
   container and flips it back on success);
3. brings the dependencies up with ``--no-recreate``, which starts exited or
   absent containers and never recreates an existing one; the agent is left out
   so its stale container is neither started nor waited on before step 4, and so
   is every one-shot dependency step 1 already ran to a zero exit, which this
   ``up`` would otherwise start a second time;
4. recreates only the agent with ``--force-recreate --no-deps agent``: auth
   overlay bind mounts are bound at container creation, and #1090's setup
   replay keys off the agent's new identity. A failure here raises
   ``AgentRecreateError``.

A resume passes only when step 4 ran and succeeded. Every earlier failure either
still reaches step 4 (a dependency start or up failure on a usable runtime) or
raises, and the caller counts every raised error as a failed restart.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Final

from awf.common.audit import redact_audit_text
from awf.control.executor.constants import (
    MONITOR_RESUME_DEPENDENCY_PREREQUISITE_FAILED_REASON_CODE,
    MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT_REASON_CODE,
)
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
_EXITED_SUCCESSFULLY: Final = re.compile(r"^exited\s*\(\s*0\s*\)(?:\s|$)")
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
    """The final agent force-recreate failed, so compose never confirmed a fresh agent.

    The agent may lack the refreshed auth overlays and env and a new identity
    for #1090's setup replay, so the caller counts it as a failed restart
    however healthy the old stack looks.
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
) -> list[RuntimeService]:
    """Return the containers of declared non-agent services.

    ``compose run`` containers and orphans of services no longer in compose.yml
    never settle on their own; ``declared_services`` is ``None`` when compose.yml
    is unreadable, and then every non-agent service counts.
    """
    return [
        service
        for service in snapshot.services
        if service.name != AGENT_SERVICE_NAME
        and not service.one_off
        and (declared_services is None or service.name in declared_services)
    ]


def _recovering_dependencies(
    snapshot: RuntimeSnapshot,
    declared_services: tuple[str, ...] | None,
) -> dict[str, str]:
    """Return running dependencies whose health is not settled yet."""
    recovering: dict[str, str] = {}
    for service in _dependency_containers(snapshot, declared_services):
        if service.state.strip().lower() != "running":
            continue
        health = (service.health or "").strip().lower()
        if health in _RECOVERING_HEALTH:
            recovering[service.name] = health
    return recovering


def _stopped_dependencies(
    snapshot: RuntimeSnapshot,
    declared_services: tuple[str, ...] | None,
    recreated_services: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return dependencies whose container is not running or, when declared, absent.

    An absent container (a released runtime) would otherwise be created by the
    ``up --wait`` and gated on the healthcheck retries of an older compose.yml.
    Services the resume already recreated or started are never started again.
    """
    containers = _dependency_containers(snapshot, declared_services)
    stopped = {
        service.name for service in containers if service.state.strip().lower() in _STOPPED_STATES
    }
    if declared_services is not None:
        present = {service.name for service in containers}
        stopped.update(
            name for name in declared_services if name != AGENT_SERVICE_NAME and name not in present
        )
    return tuple(sorted(stopped - set(recreated_services)))


def _stopped_prerequisites(
    snapshot: RuntimeSnapshot,
    declared_services: tuple[str, ...] | None,
    services: tuple[str, ...],
    dependency_conditions: Mapping[str, Mapping[str, str]],
) -> set[str]:
    """Return the stopped or absent dependencies ``services`` transitively depend on.

    A ``--no-deps`` recreate never starts them, so they start before their dependents.
    The walk passes through running prerequisites, which may wait on a stopped one.
    """
    stopped = set(_stopped_dependencies(snapshot, declared_services, services))
    visited = set(services)
    frontier = list(services)
    while frontier:
        for prerequisite in dependency_conditions.get(frontier.pop(), {}):
            if prerequisite not in visited:
                visited.add(prerequisite)
                frontier.append(prerequisite)
    return visited & stopped


def _prerequisite_progress(
    name: str,
    *,
    containers: Mapping[str, RuntimeService],
    pending: set[str],
    dependency_conditions: Mapping[str, Mapping[str, str]],
) -> tuple[bool, dict[str, str], dict[str, str]]:
    """Return whether ``name`` may start, which prerequisites settle, and which failed.

    A prerequisite still waiting to be started blocks it; one that failed blocks
    it without being waited on and is returned with its status. A gone one, the
    agent and undeclared services never block.
    """
    ready = True
    settling: dict[str, str] = {}
    failed: dict[str, str] = {}
    for prerequisite, condition in dependency_conditions.get(name, {}).items():
        if prerequisite in pending:
            ready = False
            continue
        container = containers.get(prerequisite)
        if container is None:
            continue
        state = container.state.strip().lower()
        health = (container.health or "").strip().lower()
        exited_ok = (
            state == "exited"
            and _EXITED_SUCCESSFULLY.match((container.status or "").strip().lower()) is not None
        )
        if condition == "service_completed_successfully":
            satisfied, progress = exited_ok, "running" if state == "running" else None
        elif condition == "service_healthy":
            satisfied = state == "running" and health not in _RECOVERING_HEALTH
            progress = health if state == "running" and not satisfied else None
        else:
            satisfied, progress = state == "running" or exited_ok, None
        if not satisfied:
            ready = False
            if progress is not None:
                settling[prerequisite] = progress
            else:
                failed[prerequisite] = (container.status or state).strip()
    return ready, settling, failed


def _completed_dependencies(
    snapshot: RuntimeSnapshot | None,
    declared_services: tuple[str, ...] | None,
    started_services: set[str],
) -> set[str]:
    """Return the dependencies this resume started that have since exited with code 0.

    A one-shot job (``service_completed_successfully``) started or recreated by
    the resume has already run; another ``up`` would start its container again.
    """
    if snapshot is None:
        return set()
    return {
        service.name
        for service in _dependency_containers(snapshot, declared_services)
        if service.name in started_services
        and service.state.strip().lower() == "exited"
        and _EXITED_SUCCESSFULLY.match((service.status or "").strip().lower()) is not None
    }


async def wait_for_recovering_dependencies(
    compose_project: str,
    *,
    workspace_id: str,
    budget_seconds: int,
    start_stopped: Callable[[tuple[str, ...]], Awaitable[None]] | None = None,
    declared_services: tuple[str, ...] | None = None,
    recreated_services: tuple[str, ...] = (),
    dependency_conditions: Mapping[str, Mapping[str, str]] | None = None,
    deadline: float | None = None,
) -> RuntimeSnapshot | None:
    """Wait until no running dependency is ``starting``/``unhealthy``, within the budget.

    ``start_stopped`` receives the dependencies stopped in the first snapshot,
    other than the ``recreated_services`` already recreated or started; once it
    has started them they are waited on like any other running dependency. With
    ``dependency_conditions`` (service -> prerequisite -> compose ``depends_on``
    condition) each one is started only once its prerequisites are satisfied,
    and the wait also covers prerequisites a still-unstarted dependency needs.

    Raises ``DependencyWaitTimeoutError`` (``MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT``)
    naming each unsettled service when the budget runs out. An unreadable runtime
    is logged and skipped: the compose call that follows surfaces the real error.
    Returns the settled snapshot, or ``None`` when the runtime was unreadable.
    ``deadline`` (a ``_monotonic`` instant) lets an earlier step share the budget.
    """
    if deadline is None:
        deadline = _monotonic() + budget_seconds
    conditions = dependency_conditions or {}
    pending: set[str] | None = None
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
            return None
        if snapshot.stack_state in _UNREADABLE_STACK_STATES:
            _log.warning(
                "executor.resume_dependency_wait_runtime_unreadable",
                workspace_id=workspace_id,
                compose_project_name=compose_project,
                stack_state=snapshot.stack_state,
            )
            return None
        settling: dict[str, str] = {}
        if start_stopped is not None:
            if pending is None:
                pending = set(
                    _stopped_dependencies(snapshot, declared_services, recreated_services)
                )
            containers = {
                service.name: service
                for service in _dependency_containers(snapshot, declared_services)
            }
            ready: list[str] = []
            for name in sorted(pending):
                can_start, waiting_on, _ = _prerequisite_progress(
                    name,
                    containers=containers,
                    pending=pending,
                    dependency_conditions=conditions,
                )
                if can_start:
                    ready.append(name)
                settling.update(waiting_on)
            if ready:
                pending.difference_update(ready)
                await start_stopped(tuple(ready))
                continue
        recovering = {**settling, **_recovering_dependencies(snapshot, declared_services)}
        if not recovering:
            return snapshot
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


async def _recreate_in_dependency_order(
    recreate: Callable[[str], Awaitable[None]],
    services: tuple[str, ...],
    *,
    start: Callable[[str], Awaitable[None]],
    compose_project: str,
    workspace_id: str,
    budget_seconds: int,
    deadline: float,
    declared_services: tuple[str, ...] | None,
    dependency_conditions: Mapping[str, Mapping[str, str]],
) -> None:
    """Force-recreate ``services``, each after the prerequisites it depends on.

    ``--no-deps`` skips every depends_on health gate, so a dependent is recreated
    only once each prerequisite, recreated here or not, satisfies its condition.
    A stopped or absent prerequisite (a released runtime) is started with ``start``
    first, in the same order, so no dependent is recreated against a missing
    service; a failed start raises like a failed recreate. The runtime is
    inspected only when nothing else can be recreated; when it is unreadable (or
    a depends_on cycle leaves nothing to wait on) the rest are recreated in depends_on order and the dependency wait and ``up`` take
    over. A prerequisite that failed with nothing left to wait on raises
    ``MONITOR_RESUME_DEPENDENCY_PREREQUISITE_FAILED`` before its dependent is
    recreated, so the dependent stays pending for the next resume.
    Raises ``DependencyWaitTimeoutError`` naming the settling prerequisites when
    the budget runs out first.
    """
    pending = set(services)
    starts: set[str] | None = None
    gated = True
    while pending:
        # A cycle (rejected by compose) cannot block every recreate.
        unblocked = [
            name
            for name in sorted(pending)
            if not dependency_conditions.get(name, {}).keys() & pending
        ] or sorted(pending)
        ready = [name for name in unblocked if not gated or not dependency_conditions.get(name)]
        settling: dict[str, str] = {}
        if not ready:
            try:
                snapshot = await inspect_resume_runtime(compose_project)
            except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
                _log.warning(
                    "executor.resume_dependency_recreate_inspect_failed",
                    workspace_id=workspace_id,
                    compose_project_name=compose_project,
                    error=redact_audit_text(repr(exc), limit=500),
                )
                snapshot = None
            if snapshot is None or snapshot.stack_state in _UNREADABLE_STACK_STATES:
                gated = False
                continue
            if starts is None:
                starts = _stopped_prerequisites(
                    snapshot, declared_services, services, dependency_conditions
                )
                if starts:
                    pending.update(starts)
                    continue
            containers = {
                service.name: service
                for service in _dependency_containers(snapshot, declared_services)
            }
            blocked: list[str] = []
            for name in unblocked:
                can_recreate, waiting_on, failed = _prerequisite_progress(
                    name,
                    containers=containers,
                    pending=pending,
                    dependency_conditions=dependency_conditions,
                )
                if can_recreate:
                    ready.append(name)
                settling.update(waiting_on)
                blocked.extend(
                    f"{name} needs {prerequisite}={status}"
                    for prerequisite, status in sorted(failed.items())
                )
            if not ready and not settling and blocked:
                # ``up --no-recreate`` can restart the prerequisite but never recreates
                # the dependent, so a dependent recreated now would keep a failed init.
                raise ComposeOperationError(
                    operation="dependency_recreate",
                    returncode=1,
                    stdout="",
                    stderr="dependency prerequisites failed: " + ", ".join(blocked),
                    reason_code=MONITOR_RESUME_DEPENDENCY_PREREQUISITE_FAILED_REASON_CODE,
                )
            if not ready and not settling:
                gated = False
                continue
        if ready:
            for name in ready:
                await (start if starts and name in starts else recreate)(name)
                pending.discard(name)
            continue
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise DependencyWaitTimeoutError(budget_seconds=budget_seconds, recovering=settling)
        _log.info(
            "executor.resume_dependency_recreate_wait",
            workspace_id=workspace_id,
            compose_project_name=compose_project,
            recovering=settling,
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
    on_recreated: Callable[[str], None] | None = None,
    dependency_conditions: Mapping[str, Mapping[str, str]] | None = None,
) -> ComposeOperationError | None:
    """Restart the runtime for a monitor resume, recreating only what must change.

    ``dependency_services`` lists the compose services that are not the agent;
    ``None`` (unreadable compose file) falls back to bringing up the whole project.
    ``recreate_services`` are dependencies force-recreated first (env rewritten
    in compose.yml or backed by a set env secret, or still unsettled when the
    last attempt gave up), so nothing waits on them while they hold stale config;
    a failed recreate raises and is never tolerated, and ``on_recreated`` runs with
    each service as soon as its own recreate succeeds, whatever fails later (never
    for a service that was not recreated). Every other dependency is never recreated. When a dependency start or up fails, ``runtime_usable``
    decides whether the agent is still recreated (auth overlays, env and #1090's
    setup replay depend on it); the tolerated failure is returned so the caller
    can record it. A failed agent recreate raises ``AgentRecreateError``.
    ``dependency_conditions`` (compose ``depends_on``) orders the recreates and
    the stopped starts.
    """
    common: dict[str, Any] = {
        "project_name": compose_project,
        "compose_file": compose_file,
        "workspace_id": workspace_id,
        "wait": True,
        "compose_up_timeout_seconds": budget_seconds,
    }
    deadline = _monotonic() + budget_seconds
    recreated = tuple(sorted(set(recreate_services) - {AGENT_SERVICE_NAME}))
    started = set(recreated)

    async def _start_stopped(services: tuple[str, ...]) -> None:
        started.update(services)
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

    if recreated:
        _log.info(
            "executor.resume_dependency_recreate",
            workspace_id=workspace_id,
            compose_project_name=compose_project,
            services=list(recreated),
        )

        # One service per call: ``--no-deps`` then skips every depends_on health gate.
        async def _recreate(service: str) -> None:
            await compose.ensure_project_up(
                **{**common, "wait": False},
                force_recreate=True,
                no_deps=True,
                services=(service,),
            )
            if on_recreated is not None:
                on_recreated(service)

        await _recreate_in_dependency_order(
            _recreate,
            recreated,
            start=lambda service: _start_stopped((service,)),
            compose_project=compose_project,
            workspace_id=workspace_id,
            budget_seconds=budget_seconds,
            deadline=deadline,
            declared_services=dependency_services,
            dependency_conditions=dependency_conditions or {},
        )

    tolerated: ComposeOperationError | None = None
    try:
        settled = await wait_for_recovering_dependencies(
            compose_project,
            workspace_id=workspace_id,
            budget_seconds=budget_seconds,
            start_stopped=_start_stopped,
            declared_services=dependency_services,
            recreated_services=tuple(sorted(started)),
            dependency_conditions=dependency_conditions,
            deadline=deadline,
        )
        if dependency_services is None:
            await compose.ensure_project_up(**common, no_recreate=True)
        else:
            # A one-shot dependency the resume already ran to completion is not started again.
            completed = _completed_dependencies(settled, dependency_services, started)
            dependencies = tuple(
                name
                for name in dependency_services
                if name != AGENT_SERVICE_NAME and name not in completed
            )
            if dependencies:
                final_options: dict[str, Any] = {}
                if completed:
                    # Compose otherwise expands depends_on and restarts completed
                    # jobs. Retain its prerequisite gate before disabling expansion.
                    assert settled is not None
                    containers = {
                        service.name: service
                        for service in _dependency_containers(settled, dependency_services)
                    }
                    for name in dependencies:
                        ready, _, _ = _prerequisite_progress(
                            name,
                            containers=containers,
                            pending=set(),
                            dependency_conditions=dependency_conditions or {},
                        )
                        if not ready:
                            raise ComposeOperationError(
                                operation="dependency_up",
                                returncode=1,
                                stdout="",
                                stderr=f"dependency prerequisites failed: {name}",
                                reason_code=MONITOR_RESUME_DEPENDENCY_PREREQUISITE_FAILED_REASON_CODE,
                            )
                    final_options["no_deps"] = True
                await compose.ensure_project_up(
                    **common, no_recreate=True, services=dependencies, **final_options
                )
    except DependencyWaitTimeoutError:
        # Never tolerated: the restart streak escalates its unsettled dependencies.
        raise
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
            **common, force_recreate=True, no_deps=True, services=(AGENT_SERVICE_NAME,)
        )
    except ComposeOperationError as exc:
        raise AgentRecreateError(exc) from exc
    return tolerated
