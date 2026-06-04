"""Liveness + readiness endpoints.

``/healthz`` is intentionally dependency-free: no DB query, no Docker daemon
check, no secrets read. Its job is to report that the HTTP stack itself is up
so external probes can distinguish "AWF process is alive" from "AWF depends on
X which is down."

``/readyz`` (PRD v2.2 §12, §18.2, §18.3) reports per-dependency readiness:
control-plane DB connectivity, Docker CLI + daemon + Compose plugin, and
presence of the configured agent runtime image. Each check has a stable
``reason`` code so dashboards and operators can route alerts on the *specific*
failing dependency rather than a generic 503.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from awf import __version__
from awf.api.deps import require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import HttpExceptionErrorResponse
from awf.common.audit import redact_audit_text
from awf.common.commands import AsyncCommandRunner, AsyncioSubprocessRunner, CommandResult
from awf.common.config import get_settings
from awf.db.repositories import EgressAuditRepository
from awf.db.resilience import (
    db_connection_failure_reason,
    invalidate_or_rollback_session,
    run_db_operation_with_retry,
)
from awf.service.config import resolve_service_settings
from awf.service.gc import DEFAULT_MIN_AGE_HOURS
from awf.service.orphan_resources import (
    ResourceScan,
    WorkspaceIdView,
    build_orphan_resource_summary,
    scan_docker_resources_async,
    scan_managed_worktrees,
    unavailable_workspace_view,
    workspace_id_view_from_session,
)
from awf.service.provider_readiness import (
    ProviderReadinessError,
    collect_agent_readiness,
    validate_provider_names,
)
from awf.service.readiness import collect_core_readiness_report

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Shape of the /healthz response.

    Declared as a Pydantic model (rather than a bare dict) so the OpenAPI spec
    documents the contract and downstream MCP/REST clients get typed bindings.
    """

    status: str
    service: str
    version: str


class CheckResult(BaseModel):
    """One readiness sub-check result.

    ``reason`` is a stable uppercase code (e.g. ``DOCKER_DAEMON_UNREACHABLE``)
    intended for routing alerts and dashboards; ``detail`` carries the raw
    error text for human eyes.
    """

    ok: bool
    status: str
    reason: str | None = None
    detail: str | None = None
    version: str | None = None
    resource_count: int | None = None
    expected_count: int | None = None
    active_count: int | None = None
    orphan_count: int | None = None
    unknown_count: int | None = None
    counts_by_kind: dict[str, int] | None = None
    orphan_counts_by_kind: dict[str, int] | None = None
    expected_counts_by_kind: dict[str, int] | None = None
    unknown_counts_by_kind: dict[str, int] | None = None
    orphan_classification_counts: dict[str, int] | None = None
    egress_posture_counts: dict[str, int] | None = None
    cleanup_readiness: dict[str, Any] | None = None
    scanners: dict[str, dict[str, Any]] | None = None
    examples: list[dict[str, Any]] | None = None


class ReadyResponse(BaseModel):
    service: str
    version: str
    status: str
    checks: dict[str, CheckResult]
    agent_readiness: dict[str, Any]


class ReleaseReadinessCheckResponse(BaseModel):
    name: str
    status: str
    reason_code: str
    message: str
    evidence: dict[str, Any]


class ReleaseReadinessResponse(BaseModel):
    status: str
    summary: dict[str, int]
    checks: list[ReleaseReadinessCheckResponse]
    next_actions: list[str]


_RELEASE_READINESS_AUTH_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **API_TOKEN_AUTH_ERROR_RESPONSES,
    503: {
        "model": HttpExceptionErrorResponse | ReleaseReadinessResponse,
        "description": "Service Unavailable",
    },
}


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", service="awf", version=__version__)


@router.get(
    "/release-readiness",
    dependencies=[Depends(require_api_token)],
    response_model=ReleaseReadinessResponse,
    responses=_RELEASE_READINESS_AUTH_ERROR_RESPONSES,
)
async def release_readiness(
    response: Response,
    provider: list[str] = Query(default=[]),
    failure_window_hours: int = Query(default=24, ge=1, le=168),
    slo_window_hours: int = Query(default=168, ge=1, le=720),
    allow_generic_failures: bool = Query(default=False),
    allow_slo_breach: bool = Query(default=False),
) -> dict[str, object]:
    """Return the executable AWF Core local release scorecard.

    This intentionally mirrors ``awf service readiness --format json`` so REST,
    CLI, and MCP clients can make the same Core-ready decision.
    """
    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    report = await collect_core_readiness_report(
        settings=resolve_service_settings(get_settings()),
        failure_window_hours=failure_window_hours,
        slo_window_hours=slo_window_hours,
        strict_providers=frozenset(strict_providers),
        allow_generic_failures=allow_generic_failures,
        allow_slo_breach=allow_slo_breach,
    )
    if report.status == "fail":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report.to_dict()


# Bound every subprocess call so a hung docker daemon can't wedge readiness probes
# behind the default uvicorn worker. 5s is generous for local docker calls; tune
# down once we have latency telemetry.
_CHECK_TIMEOUT_SECONDS = 5.0
_EGRESS_AUDIT_CANCEL_DRAIN_TIMEOUT_SECONDS = 0.1
_EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR = "egress_audit_summary_counts_task"


class EgressAuditSummaryInFlightError(TimeoutError):
    """Raised when a previous egress audit summary lookup is still running."""


def _get_command_runner_for_request(request: Request) -> AsyncCommandRunner:
    """Resolve the command runner for the request.

    Tests inject a ``FakeCommandRunner`` via ``app.state.command_runner``;
    production falls back to the real asyncio subprocess runner.
    """
    runner: AsyncCommandRunner | None = getattr(request.app.state, "command_runner", None)
    if runner is None:
        return AsyncioSubprocessRunner()
    return runner


def _truncate(value: str, *, limit: int = 240) -> str:
    """Cap detail strings so a verbose docker error can't bloat the response body."""
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _redacted_check_detail(exc: Exception, *, limit: int = 240) -> str:
    return _truncate(redact_audit_text(f"{type(exc).__name__}: {exc}"), limit=limit)


async def _check_db(factory: Any) -> CheckResult:
    if factory is None:
        return CheckResult(
            ok=False,
            status="fail",
            reason="DB_NOT_CONFIGURED",
            detail="db_session_factory is not attached to app.state",
        )
    try:
        session = factory()
    except Exception as exc:
        # factory() can raise before we ever get a session — e.g. pool exhausted,
        # bad DSN, engine misconfig. Surface it as a structured DB failure so
        # /readyz returns 503 instead of an unhandled 500.
        return CheckResult(
            ok=False,
            status="fail",
            reason=db_connection_failure_reason(exc),
            detail=_truncate(f"{type(exc).__name__}: {exc}"),
        )
    try:
        try:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")), timeout=_CHECK_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            await invalidate_or_rollback_session(session, exc)
            return CheckResult(
                ok=False,
                status="fail",
                reason="DB_TIMEOUT",
                detail=f"SELECT 1 exceeded {_CHECK_TIMEOUT_SECONDS}s",
            )
        except Exception as exc:
            await invalidate_or_rollback_session(session, exc)
            return CheckResult(
                ok=False,
                status="fail",
                reason=db_connection_failure_reason(exc),
                detail=_truncate(f"{type(exc).__name__}: {exc}"),
            )
    finally:
        close = getattr(session, "close", None)
        if close is not None:
            # Close-time errors aren't actionable for a probe.
            with contextlib.suppress(Exception):
                await close()
    return CheckResult(ok=True, status="ok")


async def _run_bounded(runner: AsyncCommandRunner, args: list[str]) -> CommandResult | Exception:
    """Run a subprocess with a timeout. Returns the exception object on failure
    so the caller can map it to a structured reason code."""
    try:
        return await asyncio.wait_for(runner.run(args), timeout=_CHECK_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        return exc
    except Exception as exc:
        return exc


async def _docker_check(
    runner: AsyncCommandRunner,
    *,
    args: list[str],
    description: str,
    fail_reason: str,
    timeout_reason: str,
    detail_prefix: str = "",
    cli_missing_detail: str = "docker binary not found on PATH",
) -> CheckResult:
    """Run a docker subprocess and map its outcome to a stable CheckResult.

    Every docker dependency follows the same outcome → reason mapping: missing
    binary, timeout, transport/permission failure, non-zero exit. ``description``
    is the human-readable command shown in the timeout message; ``detail_prefix``
    lets per-image checks tag detail strings (e.g. ``"<image>: ..."``) so the
    operator sees which image was missing without parsing.
    """
    outcome = await _run_bounded(runner, args)
    if isinstance(outcome, FileNotFoundError):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_CLI_NOT_FOUND",
            detail=cli_missing_detail,
        )
    if isinstance(outcome, TimeoutError):
        return CheckResult(
            ok=False,
            status="fail",
            reason=timeout_reason,
            detail=f"{description} exceeded {_CHECK_TIMEOUT_SECONDS}s",
        )
    if isinstance(outcome, Exception):
        return CheckResult(
            ok=False,
            status="fail",
            reason=fail_reason,
            detail=_truncate(f"{detail_prefix}{type(outcome).__name__}: {outcome}"),
        )
    if outcome.returncode != 0:
        return CheckResult(
            ok=False,
            status="fail",
            reason=fail_reason,
            detail=_truncate(f"{detail_prefix}{outcome.stderr or outcome.stdout}"),
        )
    return CheckResult(ok=True, status="ok", version=outcome.stdout.strip() or None)


async def _check_docker_cli(runner: AsyncCommandRunner) -> CheckResult:
    return await _docker_check(
        runner,
        args=["docker", "--version"],
        description="docker --version",
        fail_reason="DOCKER_CLI_ERROR",
        timeout_reason="DOCKER_CLI_TIMEOUT",
    )


async def _check_docker_daemon(runner: AsyncCommandRunner) -> CheckResult:
    # ``--format`` keeps output to just the server version (one short line) so we
    # don't have to parse the verbose default ``docker info`` output.
    return await _docker_check(
        runner,
        args=["docker", "info", "--format", "{{.ServerVersion}}"],
        description="docker info",
        fail_reason="DOCKER_DAEMON_UNREACHABLE",
        timeout_reason="DOCKER_DAEMON_TIMEOUT",
    )


async def _check_docker_compose(runner: AsyncCommandRunner) -> CheckResult:
    return await _docker_check(
        runner,
        args=["docker", "compose", "version", "--short"],
        description="docker compose version",
        fail_reason="DOCKER_COMPOSE_NOT_AVAILABLE",
        timeout_reason="DOCKER_COMPOSE_TIMEOUT",
    )


async def _check_agent_runtime_image(runner: AsyncCommandRunner, image: str) -> CheckResult:
    return await _docker_check(
        runner,
        args=["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        description=f"docker image inspect {image}",
        fail_reason="AGENT_RUNTIME_IMAGE_MISSING",
        timeout_reason="AGENT_RUNTIME_IMAGE_TIMEOUT",
        detail_prefix=f"{image}: ",
        cli_missing_detail=f"docker binary not found on PATH (cannot inspect {image})",
    )


async def _workspace_view_for_readyz(
    factory: Any,
    *,
    min_retention_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> WorkspaceIdView:
    if factory is None:
        return unavailable_workspace_view()
    try:
        session = factory()
    except Exception:
        return unavailable_workspace_view()
    try:
        return await workspace_id_view_from_session(
            session,
            min_retention_hours=min_retention_hours,
        )
    except Exception:
        return unavailable_workspace_view()
    finally:
        close = getattr(session, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    def _consume(completed: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            completed.result()

    task.add_done_callback(_consume)


def reset_egress_audit_summary_counts_task(state: Any) -> None:
    """Clear any readiness egress-audit lookup tracked for an app instance."""
    task: asyncio.Task[dict[str, int]] | None = getattr(
        state,
        _EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR,
        None,
    )
    setattr(state, _EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR, None)
    if task is None:
        return
    if task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        return

    with contextlib.suppress(RuntimeError):
        task.cancel()
        _consume_task_result(task)


async def _drain_cancelled_task_result(task: asyncio.Task[Any], *, timeout: float) -> None:
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        return
    _consume_task_result(task)


async def _egress_audit_summary_counts(factory: Any) -> dict[str, int]:
    async def _summary_counts(session: Any) -> dict[str, int]:
        return await EgressAuditRepository(session).summary_counts_by_posture()

    return await run_db_operation_with_retry(factory, _summary_counts)


def _pending_egress_audit_summary_counts_task(
    state: Any,
) -> asyncio.Task[dict[str, int]] | None:
    task: asyncio.Task[dict[str, int]] | None = getattr(
        state,
        _EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR,
        None,
    )
    if task is not None and task.done():
        setattr(state, _EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR, None)
        return None
    return task


def _track_egress_audit_summary_counts_task(
    state: Any,
    task: asyncio.Task[dict[str, int]],
) -> None:
    setattr(state, _EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR, task)

    def _clear_tracked_task(completed: asyncio.Task[dict[str, int]]) -> None:
        # A cancelled task from a prior lookup can finish after a new lookup starts.
        # Only the completed task may clear its own tracking slot.
        if getattr(state, _EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR, None) is completed:
            setattr(state, _EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR, None)

    task.add_done_callback(_clear_tracked_task)


async def _egress_audit_summary_counts_with_timeout(
    factory: Any,
    state: Any,
) -> dict[str, int]:
    if _pending_egress_audit_summary_counts_task(state) is not None:
        raise EgressAuditSummaryInFlightError(
            "Previous egress audit summary_counts is still in flight"
        )

    task = asyncio.create_task(_egress_audit_summary_counts(factory))
    _track_egress_audit_summary_counts_task(state, task)
    try:
        done, _pending = await asyncio.wait({task}, timeout=_CHECK_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        task.cancel()
        await _drain_cancelled_task_result(
            task,
            timeout=_EGRESS_AUDIT_CANCEL_DRAIN_TIMEOUT_SECONDS,
        )
        raise

    if task in done:
        return task.result()

    task.cancel()
    await _drain_cancelled_task_result(
        task,
        timeout=_EGRESS_AUDIT_CANCEL_DRAIN_TIMEOUT_SECONDS,
    )
    raise TimeoutError("egress audit summary timed out")


async def _check_orphan_resources(
    *,
    runner: AsyncCommandRunner,
    factory: Any,
    work_dir: str,
    db_check: CheckResult,
    docker_check: CheckResult,
    min_retention_hours: float = DEFAULT_MIN_AGE_HOURS,
    auto_cleanup_orphans: bool = False,
) -> CheckResult:
    if not db_check.ok:
        workspace_view = unavailable_workspace_view()
    else:
        workspace_view = await _workspace_view_for_readyz(
            factory,
            min_retention_hours=min_retention_hours,
        )

    if not docker_check.ok:
        docker_scan = _docker_resource_scan_unavailable(docker_check)
    else:
        docker_scan = await scan_docker_resources_async(
            runner=runner,
            timeout=_CHECK_TIMEOUT_SECONDS,
        )

    worktree_scan = await asyncio.to_thread(scan_managed_worktrees, work_dir)
    return _orphan_resources_check_result(
        db_check=db_check,
        docker_check=docker_check,
        workspace_view=workspace_view,
        docker_scan=docker_scan,
        worktree_scan=worktree_scan,
        auto_cleanup_orphans=auto_cleanup_orphans,
    )


def _docker_resource_scan_unavailable(docker_check: CheckResult) -> ResourceScan:
    return ResourceScan(
        ok=False,
        status="unavailable",
        reason="DOCKER_RESOURCE_SCAN_UNAVAILABLE",
        detail=docker_check.detail or docker_check.reason,
    )


def _orphan_resources_check_result(
    *,
    db_check: CheckResult,
    docker_check: CheckResult,
    workspace_view: WorkspaceIdView,
    docker_scan: ResourceScan,
    worktree_scan: ResourceScan,
    auto_cleanup_orphans: bool = False,
) -> CheckResult:
    if not db_check.ok:
        workspace_view = unavailable_workspace_view()
    if not docker_check.ok:
        docker_scan = _docker_resource_scan_unavailable(docker_check)

    summary = build_orphan_resource_summary(
        docker_scan=docker_scan,
        worktree_scan=worktree_scan,
        workspace_view=workspace_view,
        auto_cleanup_orphans=auto_cleanup_orphans,
    )
    return CheckResult(**summary.to_dict())


async def _cancel_unneeded_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _check_orphan_resources_with_concurrent_scans(
    *,
    db_check_task: asyncio.Task[CheckResult],
    docker_check_task: asyncio.Task[CheckResult],
    workspace_view_task: asyncio.Task[WorkspaceIdView],
    docker_scan_task: asyncio.Task[ResourceScan],
    worktree_scan_task: asyncio.Task[ResourceScan],
    auto_cleanup_orphans: bool = False,
) -> CheckResult:
    db_check, docker_check = await asyncio.gather(db_check_task, docker_check_task)

    if db_check.ok:
        workspace_view = await workspace_view_task
    else:
        workspace_view = unavailable_workspace_view()
        await _cancel_unneeded_task(workspace_view_task)

    if docker_check.ok:
        docker_scan = await docker_scan_task
    else:
        docker_scan = _docker_resource_scan_unavailable(docker_check)
        await _cancel_unneeded_task(docker_scan_task)

    worktree_scan = await worktree_scan_task
    return _orphan_resources_check_result(
        db_check=db_check,
        docker_check=docker_check,
        workspace_view=workspace_view,
        docker_scan=docker_scan,
        worktree_scan=worktree_scan,
        auto_cleanup_orphans=auto_cleanup_orphans,
    )


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(
    request: Request,
    response: Response,
    provider: list[str] = Query(default=[]),
) -> ReadyResponse:
    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    settings = get_settings()
    service_settings = resolve_service_settings(settings)
    runner = _get_command_runner_for_request(request)
    factory = getattr(request.app.state, "db_session_factory", None)

    async def _check_egress_audit() -> CheckResult:
        if factory is None:
            return CheckResult(
                ok=True,
                status="unknown",
                reason="EGRESS_AUDIT_UNAVAILABLE",
                detail="No session factory available",
            )
        try:
            counts = await _egress_audit_summary_counts_with_timeout(
                factory,
                request.app.state,
            )
            posture_counts = {str(posture): int(count) for posture, count in counts.items()}
            total = sum(posture_counts.values())
            return CheckResult(
                ok=True,
                status="ok",
                reason="EGRESS_AUDIT_AVAILABLE",
                detail="Egress audit evidence is available",
                resource_count=total,
                egress_posture_counts=posture_counts,
            )
        except EgressAuditSummaryInFlightError:
            return CheckResult(
                ok=True,
                status="unknown",
                reason="EGRESS_AUDIT_IN_FLIGHT",
                detail="Previous egress audit summary_counts is still in flight",
                resource_count=0,
                egress_posture_counts={},
            )
        except TimeoutError:
            return CheckResult(
                ok=True,
                status="unknown",
                reason="EGRESS_AUDIT_TIMEOUT",
                detail=f"Egress audit summary_counts exceeded {_CHECK_TIMEOUT_SECONDS}s",
                resource_count=0,
                egress_posture_counts={},
            )
        except Exception as exc:
            return CheckResult(
                ok=True,
                status="unknown",
                reason="EGRESS_AUDIT_UNAVAILABLE",
                detail=_redacted_check_detail(exc),
                resource_count=0,
                egress_posture_counts={},
            )

    # Run checks concurrently so the worst-case latency stays bounded by the
    # single _CHECK_TIMEOUT_SECONDS rather than summing across dependencies (a
    # k8s/uptime probe with multiple slow deps would otherwise time out at the
    # orchestrator). Orphan detection starts its read-only scans in the same
    # wave, then gates classification on DB and daemon readiness results.
    db_check_task: asyncio.Task[CheckResult] = asyncio.create_task(_check_db(factory))
    cli_check_task: asyncio.Task[CheckResult] = asyncio.create_task(_check_docker_cli(runner))
    daemon_check_task: asyncio.Task[CheckResult] = asyncio.create_task(_check_docker_daemon(runner))
    workspace_view_task: asyncio.Task[WorkspaceIdView] = asyncio.create_task(
        _workspace_view_for_readyz(
            factory,
            min_retention_hours=service_settings.completed_workspace_retention_hours,
        )
    )
    docker_scan_task: asyncio.Task[ResourceScan] = asyncio.create_task(
        scan_docker_resources_async(
            runner=runner,
            timeout=_CHECK_TIMEOUT_SECONDS,
        )
    )
    worktree_scan_task: asyncio.Task[ResourceScan] = asyncio.create_task(
        asyncio.to_thread(scan_managed_worktrees, service_settings.work_dir)
    )
    orphan_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_orphan_resources_with_concurrent_scans(
            db_check_task=db_check_task,
            docker_check_task=daemon_check_task,
            workspace_view_task=workspace_view_task,
            docker_scan_task=docker_scan_task,
            worktree_scan_task=worktree_scan_task,
            auto_cleanup_orphans=service_settings.auto_cleanup_orphans,
        )
    )
    compose_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_docker_compose(runner)
    )
    image_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_agent_runtime_image(runner, settings.agent_runtime_image)
    )
    agent_readiness_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(
        asyncio.to_thread(
            collect_agent_readiness,
            service_settings,
            validated_strict_providers=strict_providers,
        )
    )
    egress_audit_task: asyncio.Task[CheckResult] = asyncio.create_task(_check_egress_audit())
    await asyncio.gather(
        db_check_task,
        cli_check_task,
        daemon_check_task,
        compose_check_task,
        image_check_task,
        agent_readiness_task,
        orphan_check_task,
        egress_audit_task,
    )
    db_check = db_check_task.result()
    cli_check = cli_check_task.result()
    daemon_check = daemon_check_task.result()
    compose_check = compose_check_task.result()
    image_check = image_check_task.result()
    agent_readiness = agent_readiness_task.result()
    orphan_check = orphan_check_task.result()
    egress_audit = egress_audit_task.result()
    checks = {
        "db": db_check,
        "docker_cli": cli_check,
        "docker_daemon": daemon_check,
        "docker_compose": compose_check,
        "agent_runtime_image": image_check,
        "orphan_resources": orphan_check,
        "egress_audit": egress_audit,
    }

    overall_ok = all(check.ok for check in checks.values()) and agent_readiness["status"] == "ok"
    if not overall_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyResponse(
        service="awf",
        version=__version__,
        status="ok" if overall_ok else "fail",
        checks=checks,
        agent_readiness=agent_readiness,
    )
