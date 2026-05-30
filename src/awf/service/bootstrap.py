"""Repeatable local service bootstrap orchestration."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib.resources import files
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict

from awf.service.config import (
    LOCAL_SERVICE_COMPOSE_ENV_FILE,
    LOCAL_SERVICE_COMPOSE_FILE,
    ServiceSettings,
    local_service_environ,
    resolve_local_service_compose_env_file,
)
from awf.service.environment import (
    cleared_docker_cli_client_keys,
    env_lookup,
    non_empty_env_value,
)
from awf.service.status import collect_service_status

DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 180.0
DEFAULT_BOOTSTRAP_POLL_INTERVAL_SECONDS = 2.0
AGENT_RUNTIME_DOCKERFILE = Path("docker/agent-runtime.Dockerfile")
PACKAGED_BOOTSTRAP_ASSET_ROOT = Path("bootstrap_assets")


class CompletedProcessLike(Protocol):
    @property
    def returncode(self) -> int: ...  # pragma: no cover

    @property
    def stdout(self) -> str | None: ...  # pragma: no cover

    @property
    def stderr(self) -> str | None: ...  # pragma: no cover


class SubprocessRun(Protocol):
    """Callable protocol for running bootstrap subprocess commands."""

    def __call__(
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        env: Mapping[str, str] | None = None,
    ) -> CompletedProcessLike:
        """Run a command and return a completed-process-like object."""
        ...  # pragma: no cover


class StatusCollector(Protocol):
    def __call__(
        self,
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
        compose_file: Path | None = None,
        compose_env_file: Path | None = None,
    ) -> Awaitable[dict[str, object]]: ...  # pragma: no cover


Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]


class _SubprocessRunKwargs(TypedDict):
    """Keyword arguments forwarded to the injectable subprocess runner."""

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
    force_rebuild: bool = False
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
    env_file: Path | None = None,
    asset_root: Path | None = None,
    run_subprocess: SubprocessRun | None = None,
    status_collector: StatusCollector | None = None,
    sleep: Sleep = asyncio.sleep,
    monotonic: Monotonic = time.monotonic,
    service_environ: Mapping[str, str] | None = None,
    provider_environ: Mapping[str, str] | None = None,
) -> ServiceBootstrapResult:
    """Start local service dependencies and wait for healthy status.

    ``asset_root`` pins compose, agent-runtime, and env-file resolution to an
    explicitly selected bootstrap asset root (e.g. a verified source checkout)
    instead of running discovery. ``None`` preserves discovery behavior.
    """

    resolved_options = options or ServiceBootstrapOptions()
    runner = run_subprocess or _run_subprocess
    collector = status_collector or collect_service_status
    completed: list[ServiceBootstrapStageResult] = []
    assets = _resolve_bootstrap_assets(
        compose_file,
        require_agent_runtime=not resolved_options.skip_agent_runtime_build,
        asset_root=asset_root,
    )
    if env_file is not None:
        assets = replace(assets, compose_env_file=env_file if env_file.exists() else None)
    resolved_env_file = (
        env_file
        if env_file is not None and env_file.exists()
        else _bootstrap_environment_file(assets)
    )
    raw_service_env = (
        dict(service_environ)
        if service_environ is not None
        else local_service_environ(env_file=resolved_env_file)
    )
    if provider_environ is not None:
        raw_service_env.update(provider_environ)
    service_env = _docker_cli_environ(raw_service_env)

    subprocess_env = _docker_cli_environ({**os.environ, **raw_service_env})
    for stage in _bootstrap_stages(
        settings,
        options=resolved_options,
        compose_file=compose_file,
        assets=assets,
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
        environ=service_env,
        compose_file=assets.compose_file,
        compose_env_file=assets.compose_env_file,
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
    assets: _BootstrapAssets | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[_BootstrapStage, ...]:
    """Return the ordered Docker stages required for service bootstrap."""

    resolved_assets = assets or _resolve_bootstrap_assets(
        compose_file,
        require_agent_runtime=not options.skip_agent_runtime_build,
    )
    stages: list[_BootstrapStage] = []
    if not options.skip_agent_runtime_build:
        if (
            resolved_assets.root is None or resolved_assets.agent_runtime_dockerfile is None
        ):  # pragma: no cover
            raise _bootstrap_assets_not_found_error(compose_file)
        stages.append(
            _BootstrapStage(
                "agent_runtime_build",
                (
                    "docker",
                    "build",
                    *(("--no-cache",) if options.force_rebuild else ()),
                    "-t",
                    settings.agent_runtime_image,
                    "-f",
                    str(resolved_assets.agent_runtime_dockerfile),
                    str(resolved_assets.root),
                ),
            )
        )

    compose = _compose_command(
        resolved_assets.compose_file,
        compose_env_file=resolved_assets.compose_env_file,
    )
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
    """Return whether a Compose profile is enabled in the service env."""

    _, raw = env_lookup(environ, "COMPOSE_PROFILES")
    return profile in {
        item.strip() for chunk in raw.split(",") for item in chunk.split() if item.strip()
    }


def _docker_cli_environ(
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Return subprocess env with Docker host selection from service settings."""

    resolved = dict(environ)
    # Keep Docker CLI host selection scoped to the resolved service environment;
    # falling back to ServiceSettings would reintroduce process-environment drift.
    docker_host = non_empty_env_value(resolved, "AWF_DOCKER_HOST") or non_empty_env_value(
        resolved, "DOCKER_HOST"
    )
    scrubbed_keys = {"AWF_DOCKER_HOST", *cleared_docker_cli_client_keys(environ)}
    caller_docker_host_found, caller_docker_host_value = env_lookup(os.environ, "DOCKER_HOST")
    docker_host_found, docker_host_value = env_lookup(resolved, "DOCKER_HOST")
    clears_docker_host = (
        docker_host_found
        and not docker_host_value
        and caller_docker_host_found
        and bool(caller_docker_host_value)
    )
    if docker_host or clears_docker_host:
        scrubbed_keys.update({"DOCKER_CONTEXT", "DOCKER_HOST"})
    for key in list(resolved):
        if key.upper() in scrubbed_keys:
            del resolved[key]
    if docker_host:
        resolved["DOCKER_HOST"] = docker_host
    return resolved


def _resolve_bootstrap_assets(
    compose_file: Path,
    *,
    require_agent_runtime: bool,
    asset_root: Path | None = None,
) -> _BootstrapAssets:
    """Resolve compose, runtime Dockerfile, and env-file assets for bootstrap.

    When ``asset_root`` is provided, resolution is pinned to that root instead of
    running discovery, so explicitly selected source-checkout assets are used.
    """

    if asset_root is not None:
        return _resolve_pinned_bootstrap_assets(
            compose_file,
            require_agent_runtime=require_agent_runtime,
            asset_root=asset_root,
        )

    asset_root = _resolve_bootstrap_asset_root()
    default_compose = compose_file == LOCAL_SERVICE_COMPOSE_FILE or (
        compose_file.is_absolute()
        and asset_root is not None
        and compose_file.resolve() == (asset_root / LOCAL_SERVICE_COMPOSE_FILE).resolve()
    )

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


def _resolve_pinned_bootstrap_assets(
    compose_file: Path,
    *,
    require_agent_runtime: bool,
    asset_root: Path,
) -> _BootstrapAssets:
    """Resolve bootstrap assets pinned to an explicitly selected asset root.

    Used when start selects verified source-checkout assets. The root must be a
    valid bootstrap asset root, otherwise the existing not-found error is raised
    (start maps it to ``START_COMPOSE_ASSETS_MISSING``).
    """

    if not _is_bootstrap_asset_root(asset_root):
        raise _bootstrap_assets_not_found_error(compose_file)

    default_compose = compose_file == LOCAL_SERVICE_COMPOSE_FILE or (
        compose_file.is_absolute()
        and compose_file.resolve() == (asset_root / LOCAL_SERVICE_COMPOSE_FILE).resolve()
    )
    resolved_compose_file = (
        asset_root / LOCAL_SERVICE_COMPOSE_FILE
        if default_compose
        else _resolve_user_path(compose_file)
    )
    agent_runtime_dockerfile = (
        asset_root / AGENT_RUNTIME_DOCKERFILE if require_agent_runtime else None
    )
    return _BootstrapAssets(
        root=asset_root,
        agent_runtime_dockerfile=agent_runtime_dockerfile,
        compose_file=resolved_compose_file,
        compose_env_file=_resolve_compose_env_file(asset_root),
    )


def _bootstrap_environment_file(assets: _BootstrapAssets) -> Path:
    """Return the env file path bootstrap should use as its base environment."""

    if assets.compose_env_file is not None:
        return assets.compose_env_file
    if assets.root is not None and is_packaged_bootstrap_asset_root(assets.root):
        return Path(".env")
    if assets.root is not None:
        return assets.root / LOCAL_SERVICE_COMPOSE_ENV_FILE
    return LOCAL_SERVICE_COMPOSE_ENV_FILE


def get_bootstrap_asset_root() -> Path | None:
    """Return the verified source root that contains local bootstrap assets."""

    return _resolve_bootstrap_asset_root()


def is_packaged_bootstrap_asset_root(path: Path) -> bool:
    """Return true when ``path`` is AWF's bundled wheel bootstrap asset root."""

    packaged_root = _packaged_bootstrap_asset_root()
    return packaged_root is not None and path.resolve() == packaged_root.resolve()


def _resolve_bootstrap_asset_root() -> Path | None:
    for candidate in _bootstrap_asset_root_candidates():
        if _is_bootstrap_asset_root(candidate):
            return candidate
    packaged_root = _packaged_bootstrap_asset_root()
    if packaged_root is not None:
        return packaged_root
    return None


def _packaged_bootstrap_asset_root() -> Path | None:
    """Return the bundled bootstrap asset root from an installed package."""

    try:
        candidate = files("awf").joinpath(PACKAGED_BOOTSTRAP_ASSET_ROOT.as_posix())
    except (ModuleNotFoundError, TypeError):
        return None
    # files("awf") yields a Traversable that is only a real Path for
    # filesystem installs (the wheel/editable case). Zip-imported packages
    # return a non-Path Traversable lacking Path APIs such as .resolve() that
    # callers (e.g. is_packaged_bootstrap_asset_root) rely on, so the guard
    # must stay this strict — do not broaden it to accept any Traversable.
    if not isinstance(candidate, Path):
        return None
    return candidate if _is_bootstrap_asset_root(candidate) else None


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
    """Resolve a user-provided path after expanding the home directory."""

    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return expanded.resolve()


def _resolve_compose_env_file(asset_root: Path | None) -> Path | None:
    """Return the local service compose env file when it exists."""

    if asset_root is not None:
        if is_packaged_bootstrap_asset_root(asset_root):
            return None
        candidate = asset_root / LOCAL_SERVICE_COMPOSE_ENV_FILE
        return candidate if candidate.exists() else None

    return resolve_local_service_compose_env_file(
        LOCAL_SERVICE_COMPOSE_ENV_FILE,
    )


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
    """Build the Docker Compose command prefix for a compose file."""

    args = ["docker", "compose"]
    if compose_env_file is not None:
        args.extend(["--env-file", str(compose_env_file)])
    args.extend(["-f", str(compose_file)])
    return tuple(args)


def _bootstrap_subprocess_env(environ: Mapping[str, str]) -> dict[str, str] | None:
    """Return ``environ`` as a dict, or ``None`` when it adds nothing beyond current env."""
    env_dict = dict(environ)
    if env_dict == dict(os.environ):
        return None
    return env_dict


def _run_stage(
    stage: _BootstrapStage,
    *,
    run_subprocess: SubprocessRun,
    environ: Mapping[str, str],
) -> ServiceBootstrapStageResult:
    """Run one bootstrap stage and normalize subprocess failures."""

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
    environ: Mapping[str, str],
    compose_file: Path,
    compose_env_file: Path | None,
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
                environ=environ,
                compose_file=compose_file,
                compose_env_file=compose_env_file,
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
    """Return a failed status payload for readiness collection errors."""

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
    """Run a subprocess using the same keyword filtering as test doubles."""

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
    """Build subprocess runner kwargs while omitting absent env overrides."""

    kwargs: _SubprocessRunKwargs = {
        "check": check,
        "capture_output": capture_output,
        "text": text,
    }
    if env is not None:
        kwargs["env"] = dict(env)
    return kwargs
