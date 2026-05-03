"""Repeatable local service bootstrap orchestration."""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from awf.service.config import ServiceSettings, local_service_environ
from awf.service.logs import LOCAL_SERVICE_COMPOSE_FILE
from awf.service.status import collect_service_status

DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 180.0
DEFAULT_BOOTSTRAP_POLL_INTERVAL_SECONDS = 2.0
AGENT_RUNTIME_DOCKERFILE = Path("docker/agent-runtime.Dockerfile")


class CompletedProcessLike(Protocol):
    @property
    def returncode(self) -> int: ...  # pragma: no cover

    @property
    def stdout(self) -> str | None: ...  # pragma: no cover

    @property
    def stderr(self) -> str | None: ...  # pragma: no cover


class SubprocessRun(Protocol):
    def __call__(
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
    ) -> CompletedProcessLike: ...  # pragma: no cover


class StatusCollector(Protocol):
    def __call__(
        self,
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
    ) -> Awaitable[dict[str, object]]: ...  # pragma: no cover


Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]


@dataclass(frozen=True, kw_only=True)
class ServiceBootstrapOptions:
    """Operator-tunable bootstrap settings."""

    timeout_seconds: float = DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_BOOTSTRAP_POLL_INTERVAL_SECONDS
    skip_agent_runtime_build: bool = False
    strict_providers: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, kw_only=True)
class ServiceBootstrapStageResult:
    """Recorded result for one bootstrap stage."""

    stage: str
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True, kw_only=True)
class ServiceBootstrapResult:
    """Successful bootstrap payload."""

    stages: tuple[ServiceBootstrapStageResult, ...]
    service_status: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "reason_code": "SERVICE_BOOTSTRAP_SUCCEEDED",
            "stages": [stage.to_dict() for stage in self.stages],
            "service_status": self.service_status,
        }


class ServiceBootstrapError(RuntimeError):
    """Structured bootstrap failure for clean CLI rendering."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        stage: str | None = None,
        command: Sequence[str] | None = None,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        last_status: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.stage = stage
        self.command = tuple(command or ())
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.last_status = dict(last_status) if last_status is not None else None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "failed",
            "reason_code": self.reason_code,
            "message": self.message,
        }
        if self.stage is not None:
            payload["stage"] = self.stage
        if self.command:
            payload["command"] = list(self.command)
        if self.returncode is not None:
            payload["returncode"] = self.returncode
        if self.stdout:
            payload["stdout"] = self.stdout
        if self.stderr:
            payload["stderr"] = self.stderr
        if self.last_status is not None:
            payload["last_status"] = self.last_status
        return payload


@dataclass(frozen=True)
class _BootstrapStage:
    name: str
    command: tuple[str, ...]


async def run_service_bootstrap(
    settings: ServiceSettings,
    *,
    options: ServiceBootstrapOptions | None = None,
    compose_file: Path = LOCAL_SERVICE_COMPOSE_FILE,
    run_subprocess: SubprocessRun | None = None,
    status_collector: StatusCollector | None = None,
    sleep: Sleep = asyncio.sleep,
    monotonic: Monotonic = time.monotonic,
    provider_environ: Mapping[str, str] | None = None,
) -> ServiceBootstrapResult:
    """Start local service dependencies and wait for healthy status."""

    resolved_options = options or ServiceBootstrapOptions()
    runner = run_subprocess or _run_subprocess
    collector = status_collector or collect_service_status
    completed: list[ServiceBootstrapStageResult] = []
    service_env = local_service_environ() if provider_environ is None else provider_environ

    for stage in _bootstrap_stages(
        settings,
        options=resolved_options,
        compose_file=compose_file,
        environ=service_env,
    ):
        completed.append(await asyncio.to_thread(_run_stage, stage, run_subprocess=runner))

    service_status = await _poll_status(
        settings,
        options=resolved_options,
        status_collector=collector,
        sleep=sleep,
        monotonic=monotonic,
        provider_environ=service_env,
    )
    return ServiceBootstrapResult(
        stages=tuple(completed),
        service_status=service_status,
    )


def _bootstrap_stages(
    settings: ServiceSettings,
    *,
    options: ServiceBootstrapOptions,
    compose_file: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[_BootstrapStage, ...]:
    stages: list[_BootstrapStage] = []
    if not options.skip_agent_runtime_build:
        stages.append(
            _BootstrapStage(
                "agent_runtime_build",
                (
                    "docker",
                    "build",
                    "-t",
                    settings.agent_runtime_image,
                    "-f",
                    str(AGENT_RUNTIME_DOCKERFILE),
                    ".",
                ),
            )
        )

    compose = ("docker", "compose", "-f", str(compose_file))
    stages.extend(
        [
            _BootstrapStage(
                "postgres",
                (*compose, "up", "-d", "--build", "postgres"),
            ),
            *(
                [
                    _BootstrapStage(
                        "ollama_bridge",
                        (*compose, "up", "-d", "--build", "ollama-bridge"),
                    )
                ]
                if _compose_profile_enabled(environ or {}, "ollama-bridge")
                else []
            ),
            _BootstrapStage(
                "migrate",
                (*compose, "up", "--build", "--force-recreate", "migrate"),
            ),
            _BootstrapStage(
                "api_worker",
                (*compose, "up", "-d", "--build", "api", "worker"),
            ),
        ]
    )
    return tuple(stages)


def _compose_profile_enabled(environ: Mapping[str, str], profile: str) -> bool:
    raw = environ.get("COMPOSE_PROFILES", "")
    return profile in {
        item.strip()
        for chunk in raw.split(",")
        for item in chunk.split()
        if item.strip()
    }


def _run_stage(
    stage: _BootstrapStage,
    *,
    run_subprocess: SubprocessRun,
) -> ServiceBootstrapStageResult:
    try:
        result = run_subprocess(
            list(stage.command),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ServiceBootstrapError(
            reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
            message=f"{stage.name} failed: docker binary not found on PATH",
            stage=stage.name,
            command=stage.command,
            returncode=127,
            stderr="docker binary not found on PATH",
        ) from exc
    except OSError as exc:
        detail = f"{type(exc).__name__}: {exc}"
        raise ServiceBootstrapError(
            reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
            message=f"{stage.name} failed: {detail}",
            stage=stage.name,
            command=stage.command,
            returncode=1,
            stderr=detail,
        ) from exc

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode != 0:
        raise ServiceBootstrapError(
            reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
            message=f"{stage.name} failed with exit code {result.returncode}",
            stage=stage.name,
            command=stage.command,
            returncode=result.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    return ServiceBootstrapStageResult(
        stage=stage.name,
        command=stage.command,
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
    )


async def _poll_status(
    settings: ServiceSettings,
    *,
    options: ServiceBootstrapOptions,
    status_collector: StatusCollector,
    sleep: Sleep,
    monotonic: Monotonic,
    provider_environ: Mapping[str, str],
) -> dict[str, object]:
    timeout_seconds = max(0.0, options.timeout_seconds)
    poll_interval_seconds = max(0.01, options.poll_interval_seconds)
    deadline = monotonic() + timeout_seconds
    last_status: dict[str, object] | None = None
    last_error: Exception | None = None

    while True:
        try:
            last_status = await status_collector(
                settings,
                strict_providers=options.strict_providers,
                provider_environ=provider_environ,
            )
            last_error = None
        except Exception as exc:
            last_error = exc
            last_status = _status_collection_failed_status(settings, exc)

        if last_status.get("status") == "ok":
            return last_status

        remaining = deadline - monotonic()
        if remaining <= 0:
            error = ServiceBootstrapError(
                reason_code="SERVICE_BOOTSTRAP_TIMEOUT",
                message="timed out waiting for local service readiness",
                last_status=last_status,
            )
            if last_error is not None:
                raise error from last_error
            raise error
        await sleep(min(poll_interval_seconds, remaining))


def _status_collection_failed_status(
    settings: ServiceSettings,
    exc: Exception,
) -> dict[str, object]:
    return {
        "service": settings.service_name,
        "status": "fail",
        "checks": {
            "status_collector": {
                "ok": False,
                "status": "fail",
                "reason": "STATUS_COLLECTION_FAILED",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        },
    }


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
) -> CompletedProcessLike:
    return subprocess.run(args, check=check, capture_output=capture_output, text=text)
