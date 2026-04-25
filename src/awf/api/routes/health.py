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
    session = factory()
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


async def _check_docker_cli(runner: AsyncCommandRunner) -> CheckResult:
    outcome = await _run_bounded(runner, ["docker", "--version"])
    if isinstance(outcome, FileNotFoundError):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_CLI_NOT_FOUND",
            detail="docker binary not found on PATH",
        )
    if isinstance(outcome, TimeoutError):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_CLI_TIMEOUT",
            detail=f"docker --version exceeded {_CHECK_TIMEOUT_SECONDS}s",
        )
    if isinstance(outcome, Exception):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_CLI_ERROR",
            detail=_truncate(f"{type(outcome).__name__}: {outcome}"),
        )
    if outcome.returncode != 0:
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_CLI_ERROR",
            detail=_truncate(outcome.stderr or outcome.stdout),
        )
    version = outcome.stdout.strip() or None
    return CheckResult(ok=True, status="ok", version=version)


async def _check_docker_daemon(runner: AsyncCommandRunner) -> CheckResult:
    # ``--format`` keeps output to just the server version (one short line) so we
    # don't have to parse the verbose default ``docker info`` output.
    outcome = await _run_bounded(
        runner, ["docker", "info", "--format", "{{.ServerVersion}}"]
    )
    if isinstance(outcome, FileNotFoundError):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_CLI_NOT_FOUND",
            detail="docker binary not found on PATH",
        )
    if isinstance(outcome, TimeoutError):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_DAEMON_TIMEOUT",
            detail=f"docker info exceeded {_CHECK_TIMEOUT_SECONDS}s",
        )
    if isinstance(outcome, Exception):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_DAEMON_UNREACHABLE",
            detail=_truncate(f"{type(outcome).__name__}: {outcome}"),
        )
    if outcome.returncode != 0:
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_DAEMON_UNREACHABLE",
            detail=_truncate(outcome.stderr or outcome.stdout),
        )
    return CheckResult(ok=True, status="ok", version=outcome.stdout.strip() or None)


async def _check_docker_compose(runner: AsyncCommandRunner) -> CheckResult:
    outcome = await _run_bounded(runner, ["docker", "compose", "version", "--short"])
    if isinstance(outcome, FileNotFoundError):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_CLI_NOT_FOUND",
            detail="docker binary not found on PATH",
        )
    if isinstance(outcome, TimeoutError):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_COMPOSE_TIMEOUT",
            detail=f"docker compose version exceeded {_CHECK_TIMEOUT_SECONDS}s",
        )
    if isinstance(outcome, Exception):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_COMPOSE_NOT_AVAILABLE",
            detail=_truncate(f"{type(outcome).__name__}: {outcome}"),
        )
    if outcome.returncode != 0:
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_COMPOSE_NOT_AVAILABLE",
            detail=_truncate(outcome.stderr or outcome.stdout),
        )
    return CheckResult(ok=True, status="ok", version=outcome.stdout.strip() or None)


async def _check_agent_runtime_image(runner: AsyncCommandRunner, image: str) -> CheckResult:
    outcome = await _run_bounded(
        runner, ["docker", "image", "inspect", image, "--format", "{{.Id}}"]
    )
    if isinstance(outcome, FileNotFoundError):
        return CheckResult(
            ok=False,
            status="fail",
            reason="DOCKER_CLI_NOT_FOUND",
            detail=f"docker binary not found on PATH (cannot inspect {image})",
        )
    if isinstance(outcome, TimeoutError):
        return CheckResult(
            ok=False,
            status="fail",
            reason="AGENT_RUNTIME_IMAGE_TIMEOUT",
            detail=f"docker image inspect {image} exceeded {_CHECK_TIMEOUT_SECONDS}s",
        )
    if isinstance(outcome, Exception):
        return CheckResult(
            ok=False,
            status="fail",
            reason="AGENT_RUNTIME_IMAGE_MISSING",
            detail=_truncate(f"{image}: {type(outcome).__name__}: {outcome}"),
        )
    if outcome.returncode != 0:
        return CheckResult(
            ok=False,
            status="fail",
            reason="AGENT_RUNTIME_IMAGE_MISSING",
            detail=_truncate(f"{image}: {outcome.stderr or outcome.stdout}"),
        )
    return CheckResult(ok=True, status="ok", version=outcome.stdout.strip() or None)


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(request: Request, response: Response) -> ReadyResponse:
    settings = get_settings()
    runner = _get_command_runner_for_request(request)
    factory = getattr(request.app.state, "db_session_factory", None)

    # Sequential checks: keeps the FakeCommandRunner contract simple and the
    # latency floor is dominated by docker info anyway, so concurrency wouldn't
    # buy much for a probe endpoint.
    checks = {
        "db": await _check_db(factory),
        "docker_cli": await _check_docker_cli(runner),
        "docker_daemon": await _check_docker_daemon(runner),
        "docker_compose": await _check_docker_compose(runner),
        "agent_runtime_image": await _check_agent_runtime_image(
            runner, settings.agent_runtime_image
        ),
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
