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

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from awf import __version__
from awf.common.commands import AsyncCommandRunner, AsyncioSubprocessRunner, CommandResult
from awf.common.config import get_settings

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


class ReadyResponse(BaseModel):
    service: str
    version: str
    status: str
    checks: dict[str, CheckResult]


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", service="awf", version=__version__)


# Bound every subprocess call so a hung docker daemon can't wedge readiness probes
# behind the default uvicorn worker. 5s is generous for local docker calls; tune
# down once we have latency telemetry.
_CHECK_TIMEOUT_SECONDS = 5.0


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
            reason="DB_CONNECTION_FAILED",
            detail=_truncate(f"{type(exc).__name__}: {exc}"),
        )
    try:
        try:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")), timeout=_CHECK_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return CheckResult(
                ok=False,
                status="fail",
                reason="DB_TIMEOUT",
                detail=f"SELECT 1 exceeded {_CHECK_TIMEOUT_SECONDS}s",
            )
        except Exception as exc:
            return CheckResult(
                ok=False,
                status="fail",
                reason="DB_CONNECTION_FAILED",
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


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(request: Request, response: Response) -> ReadyResponse:
    settings = get_settings()
    runner = _get_command_runner_for_request(request)
    factory = getattr(request.app.state, "db_session_factory", None)

    # Run checks concurrently so the worst-case latency stays bounded by the
    # single _CHECK_TIMEOUT_SECONDS rather than summing across all five (a
    # k8s/uptime probe with multiple slow deps would otherwise hit 25s and
    # time out at the orchestrator). Each check already returns a structured
    # CheckResult on failure, so gather() never sees an exception.
    db_check, cli_check, daemon_check, compose_check, image_check = await asyncio.gather(
        _check_db(factory),
        _check_docker_cli(runner),
        _check_docker_daemon(runner),
        _check_docker_compose(runner),
        _check_agent_runtime_image(runner, settings.agent_runtime_image),
    )
    checks = {
        "db": db_check,
        "docker_cli": cli_check,
        "docker_daemon": daemon_check,
        "docker_compose": compose_check,
        "agent_runtime_image": image_check,
    }

    overall_ok = all(check.ok for check in checks.values())
    if not overall_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyResponse(
        service="awf",
        version=__version__,
        status="ok" if overall_ok else "fail",
        checks=checks,
    )
