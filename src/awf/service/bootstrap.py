"""Repeatable local service bootstrap orchestration."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict

from awf.service.config import (
    LOCAL_SERVICE_COMPOSE_ENV_FILE,
    ServiceSettings,
    local_service_environ,
)
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
        env: Mapping[str, str] | None = None,
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


class _SubprocessRunKwargs(TypedDict):
    check: bool
    capture_output: bool
    text: Literal[True]
    env: NotRequired[Mapping[str, str]]


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


@dataclass(frozen=True)
class _BootstrapAssets:
    root: Path | None
    agent_runtime_dockerfile: Path | None
    compose_file: Path
    compose_env_file: Path | None


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
    service_env = local_service_environ()
    if provider_environ is not None:
        service_env.update(provider_environ)

    subprocess_env = {**os.environ, **service_env}
    for stage in _bootstrap_stages(
        settings,
        options=resolved_options,
        compose_file=compose_file,
        environ=subprocess_env,
    ):
        completed.append(
            await asyncio.to_thread(
                _run_stage,
                stage,
                run_subprocess=runner,
                environ=subprocess_env,
            )
        )

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
    assets = _resolve_bootstrap_assets(
        compose_file,
        require_agent_runtime=not options.skip_agent_runtime_build,
    )
    stages: list[_BootstrapStage] = []
    if not options.skip_agent_runtime_build:
        if assets.root is None or assets.agent_runtime_dockerfile is None:  # pragma: no cover
            raise _bootstrap_assets_not_found_error(compose_file)
        stages.append(
            _BootstrapStage(
                "agent_runtime_build",
                (
                    "docker",
                    "build",
                    "-t",
                    settings.agent_runtime_image,
                    "-f",
                    str(assets.agent_runtime_dockerfile),
                    str(assets.root),
                ),
            )
        )

    compose = _compose_command(assets.compose_file, compose_env_file=assets.compose_env_file)
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
        item.strip() for chunk in raw.split(",") for item in chunk.split() if item.strip()
    }


def _resolve_bootstrap_assets(
    compose_file: Path,
    *,
    require_agent_runtime: bool,
) -> _BootstrapAssets:
    asset_root = _resolve_bootstrap_asset_root()
    default_compose = compose_file == LOCAL_SERVICE_COMPOSE_FILE

    if default_compose:
        if asset_root is None:
            raise _bootstrap_assets_not_found_error(compose_file)
        resolved_compose_file = asset_root / LOCAL_SERVICE_COMPOSE_FILE
    else:
        resolved_compose_file = _resolve_user_path(compose_file)

    agent_runtime_dockerfile: Path | None = None
    if require_agent_runtime:
        if asset_root is None:
            raise _bootstrap_assets_not_found_error(compose_file)
        agent_runtime_dockerfile = asset_root / AGENT_RUNTIME_DOCKERFILE

    return _BootstrapAssets(
        root=asset_root,
        agent_runtime_dockerfile=agent_runtime_dockerfile,
        compose_file=resolved_compose_file,
        compose_env_file=_resolve_compose_env_file(asset_root),
    )


def _resolve_bootstrap_asset_root() -> Path | None:
    for candidate in _bootstrap_asset_root_candidates():
        if _is_bootstrap_asset_root(candidate):
            return candidate
    return None


def _bootstrap_asset_root_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    cwd = Path.cwd().resolve()
    candidates.extend((cwd, *cwd.parents))
    module_file = Path(__file__).resolve()
    candidates.extend(module_file.parents)

    deduplicated: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduplicated.append(candidate)
    return tuple(deduplicated)


def _is_bootstrap_asset_root(candidate: Path) -> bool:
    return (
        candidate.is_dir()
        and (candidate / AGENT_RUNTIME_DOCKERFILE).is_file()
        and (candidate / LOCAL_SERVICE_COMPOSE_FILE).is_file()
        and (candidate / "docker/control-plane.Dockerfile").is_file()
        and (candidate / "pyproject.toml").is_file()
        and (candidate / "src/awf/__init__.py").is_file()
    )


def _resolve_user_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return expanded.resolve()


def _resolve_compose_env_file(asset_root: Path | None) -> Path | None:
    if LOCAL_SERVICE_COMPOSE_ENV_FILE.is_absolute():
        return LOCAL_SERVICE_COMPOSE_ENV_FILE if LOCAL_SERVICE_COMPOSE_ENV_FILE.exists() else None
    if asset_root is not None:
        candidate = asset_root / LOCAL_SERVICE_COMPOSE_ENV_FILE
        if candidate.exists():
            return candidate
    if LOCAL_SERVICE_COMPOSE_ENV_FILE.exists():
        return LOCAL_SERVICE_COMPOSE_ENV_FILE.resolve()
    return None


def _bootstrap_assets_not_found_error(compose_file: Path) -> ServiceBootstrapError:
    return ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND",
        message=(
            "Cannot resolve AWF bootstrap assets for local service startup. "
            "Run awf service bootstrap from an AWF source checkout that contains "
            f"{AGENT_RUNTIME_DOCKERFILE} and {LOCAL_SERVICE_COMPOSE_FILE}, or install "
            "an AWF package that explicitly supports bundled bootstrap assets. "
            f"Required default compose file: {compose_file}."
        ),
    )


def _compose_command(
    compose_file: Path,
    *,
    compose_env_file: Path | None = None,
) -> tuple[str, ...]:
    args = ["docker", "compose"]
    if compose_env_file is not None:
        args.extend(["--env-file", str(compose_env_file)])
    args.extend(["-f", str(compose_file)])
    return tuple(args)


def _bootstrap_subprocess_env(environ: Mapping[str, str]) -> dict[str, str] | None:
    """Build subprocess environment overrides, or return ``None`` when unchanged."""
    if not environ:
        return None
    merged = {**os.environ, **dict(environ)}
    if merged == dict(os.environ):
        return None
    return merged


def _run_stage(
    stage: _BootstrapStage,
    *,
    run_subprocess: SubprocessRun,
    environ: Mapping[str, str],
) -> ServiceBootstrapStageResult:
    try:
        result = run_subprocess(
            list(stage.command),
            **_subprocess_run_kwargs(
                check=False,
                capture_output=True,
                text=True,
                env=_bootstrap_subprocess_env(environ),
            ),
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
    env: Mapping[str, str] | None = None,
) -> CompletedProcessLike:
    return subprocess.run(
        args,
        **_subprocess_run_kwargs(
            check=check,
            capture_output=capture_output,
            text=text,
            env=env,
        ),
    )


def _subprocess_run_kwargs(
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    env: Mapping[str, str] | None,
) -> _SubprocessRunKwargs:
    kwargs: _SubprocessRunKwargs = {
        "check": check,
        "capture_output": capture_output,
        "text": text,
    }
    if env is not None:
        kwargs["env"] = dict(env)
    return kwargs
