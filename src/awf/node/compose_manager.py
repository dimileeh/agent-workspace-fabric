"""Docker Compose provisioning for per-workspace service stacks.

Renders a per-workspace compose file from a Jinja2 template, then drives the
stack lifecycle via the ``docker compose`` CLI. One compose **project** per
workspace (project name ``awf_<workspace_id>``) so:

- Cleanup is deterministic: ``docker compose down -v`` on the right project
  removes every container, network, and volume we created.
- Two workspaces for the same repo cannot collide on container names.
- An operator can eyeball ``docker ps --filter name=awf-`` and see live stacks.

We shell out to the CLI rather than use the ``docker`` Python SDK because
Compose semantics (service dependencies, healthchecks, profiles) are a first-
class CLI feature; SDK-based implementations tend to reinvent them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from awf.common.audit import redact_audit_value
from awf.common.immutability import frozen_mapping
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets

_log = get_logger(__name__)

DOCKER_CAPTURE_TIMEOUT_SECONDS = 30.0
COMPOSE_CAPTURE_TIMEOUT_SECONDS = 360.0
COMPOSE_CAPTURE_TIMEOUT_BUFFER_SECONDS = 60.0
_DOCKER_CAPTURE_KILL_WAIT_SECONDS = 5.0
_COMPOSE_DISPATCH_RETRY_MARKERS = (
    "unknown shorthand flag: 'd' in -d",
    "unknown flag: --remove-orphans",
)

DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES = 200
"""Default number of companion log lines captured on a service-startup failure."""

SERVICE_STARTUP_DIAGNOSTICS_SCHEMA = "service_startup_diagnostics.v1"
"""Schema marker for the persisted ``SERVICE_STARTUP_FAILURE`` diagnostics payload."""

_SERVICE_STARTUP_HEALTH_LOG_TAIL_ENTRIES = 5
"""How many trailing ``.State.Health.Log`` entries to persist per companion."""


class ComposeOperationError(Exception):
    """Raised when a ``docker compose`` command exits non-zero.

    Carries stdout/stderr plus a structured reason code so the provisioner can
    convert it to a workspace failure without regex-parsing error messages.
    """

    def __init__(
        self,
        *,
        operation: str,
        returncode: int,
        stdout: str,
        stderr: str,
        reason_code: str = "COMPOSE_COMMAND_FAILED",
    ) -> None:
        """Capture the failed compose operation and its diagnostic streams."""
        self.operation = operation
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.reason_code = reason_code
        super().__init__(
            f"docker compose {operation} failed "
            f"(exit={returncode}, reason={reason_code}): "
            f"{stderr.strip() or stdout.strip() or '<no output>'}"
        )


@dataclass(frozen=True)
class AuthMount:
    """One read-only bind mount carrying a credential directory into the agent."""

    source: str
    """Absolute host path (e.g. ``/home/dimileeh/.codex``)."""

    target: str
    """Container path (e.g. ``/home/agent/.codex``)."""

    mode: str = "ro"
    """Bind mount mode. ``ro`` = read-only (the default — credentials shouldn't be mutated)."""


@dataclass(frozen=True)
class CompanionService:
    """An auxiliary service spun up in the same compose stack as the agent.

    E.g. when the agent edits aira-web, a ``backend`` companion running
    aira-agent is needed so Playwright tests can exercise the full
    BFF → backend → DB path against a live stack.

    The companion's source comes from a host checkout (typically a second
    GitManager worktree of the companion's repo at its base branch). Its
    ``.env`` file is mounted read-only so secrets never land in AWF state.
    """

    name: str
    """Service name in compose (also the DNS hostname within ``awf_net``)."""

    build_context: str
    """Absolute host path the Dockerfile's build context points at."""

    dockerfile: str = "Dockerfile"
    """Relative path inside ``build_context`` to the Dockerfile."""

    env_file: str | None = None
    """Absolute host path to a ``.env`` file (read-only)."""

    environment: tuple[tuple[str, str], ...] = ()
    """Additional env vars that override the env_file."""

    depends_on: tuple[str, ...] = ("postgres",)
    """Other service names this companion waits on before starting."""

    healthcheck_cmd: str | None = None
    """Shell command for Docker's healthcheck. When set, the agent container
    depends on ``service_healthy`` for this companion."""

    ports: tuple[tuple[int, int], ...] = ()
    """container_port → host_port. Usually empty — the agent talks over the
    internal network only."""

    command: str | None = None
    """Override the default command (e.g. ``npm run dev``)."""

    volumes: tuple[tuple[str, str], ...] = ()
    """Extra ``source:target`` bind mounts for the companion."""

    secret_metadata: Mapping[str, Any] = field(default_factory=dict)
    """Non-secret metadata about companion env secret resolution."""

    def __post_init__(self) -> None:
        """Freeze secret metadata so rendered stack records remain immutable."""
        object.__setattr__(self, "secret_metadata", frozen_mapping(self.secret_metadata))


@dataclass(frozen=True)
class ComposeService:
    """Generic profile-declared service in the outer AWF stack."""

    name: str
    image: str | None = None
    build_context: str | None = None
    dockerfile: str = "Dockerfile"
    env_file: str | None = None
    environment: tuple[tuple[str, str], ...] = ()
    depends_on: tuple[str, ...] = ()
    healthcheck_cmd: str | None = None
    ports: tuple[tuple[int, int], ...] = ()
    command: str | None = None
    volumes: tuple[tuple[str, str], ...] = ()
    privileged: bool = False
    required: bool = True


@dataclass(frozen=True)
class WorkspaceComposeSpec:
    """Everything the template needs to render one workspace stack."""

    workspace_id: str
    worktree_host_path: Path
    agent_runtime_image: str = "awf-agent-runtime:latest"
    agent_environment: tuple[tuple[str, str], ...] = ()
    docker_mode: str = "none"
    dind_image: str = "docker:27-dind"
    postgres_image: str = "postgres:16-alpine"
    postgres_user: str = "awf"
    postgres_db: str = "awf"
    postgres_password: str | None = None  # None → generate one
    cpu_limit: str | None = None
    memory_limit: str | None = None
    auth_mounts: tuple[AuthMount, ...] = ()
    git_name: str | None = None
    git_email: str | None = None
    services: tuple[ComposeService, ...] = ()
    companions: tuple[CompanionService, ...] = ()
    network_internal: bool = False
    host_gateway_enabled: bool = True
    compose_up_timeout_seconds: int = 300

    def project_name(self) -> str:
        """Return the deterministic Docker Compose project name for the workspace."""
        return f"awf_{self.workspace_id}"


@dataclass(frozen=True)
class ComposeProjectPaths:
    """Rendered compose project locations and non-secret launch metadata."""

    project_dir: Path
    compose_file: Path
    secret_lease_mount_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze metadata returned to control-plane records."""
        object.__setattr__(
            self,
            "secret_lease_mount_metadata",
            frozen_mapping(self.secret_lease_mount_metadata),
        )


def _is_transient_compose_dispatch_failure(stderr: str) -> bool:
    """Detect Docker CLI/plugin dispatch flakes where ``compose`` is dropped.

    Docker Desktop can occasionally return top-level ``docker`` flag errors for
    a valid ``docker compose ...`` argv under heavy parallel test pressure. A
    single retry is safe because Compose ``up``/``down`` are idempotent for the
    same project/file pair and avoids failing workspaces on a malformed local
    CLI dispatch.
    """
    return any(marker in stderr for marker in _COMPOSE_DISPATCH_RETRY_MARKERS)


def _compose_up_capture_timeout_seconds(compose_up_timeout_seconds: int, *, wait: bool) -> float:
    """Return AWF's outer timeout for ``docker compose up``.

    Compose applies ``--wait-timeout`` to readiness after build/recreate/start
    work. AWF's subprocess guard covers that launch phase separately so a cold
    build cannot consume the readiness wait budget before Compose gets to use it.
    """
    launch_timeout_seconds = float(compose_up_timeout_seconds)
    readiness_timeout_seconds = launch_timeout_seconds if wait else 0.0
    return (
        launch_timeout_seconds + readiness_timeout_seconds + COMPOSE_CAPTURE_TIMEOUT_BUFFER_SECONDS
    )


class ComposeManager:
    """Renders, launches, and tears down per-workspace compose stacks."""

    def __init__(self, *, work_dir: Path, template_path: Path) -> None:
        """Prepare the compose project directory and Jinja template loader."""
        self._projects_dir = work_dir / "compose"
        template_dir = template_path.parent
        self._env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            undefined=StrictUndefined,  # Fail fast on missing template vars.
            autoescape=False,  # Writing YAML, not HTML; autoescape would break quotes.
            keep_trailing_newline=True,
        )
        self._template_name = template_path.name

    # ── Public API ─────────────────────────────────────────────────────────

    def render(self, spec: WorkspaceComposeSpec) -> ComposeProjectPaths:
        """Render the compose file to disk. Does not launch anything."""
        project_dir = self._projects_dir / spec.workspace_id
        project_dir.mkdir(parents=True, exist_ok=True)
        compose_file = project_dir / "compose.yml"

        password = spec.postgres_password or secrets.token_urlsafe(18)
        resources = None
        if spec.cpu_limit or spec.memory_limit:
            resources = {
                "cpu_limit": spec.cpu_limit or "4",
                "memory_limit": spec.memory_limit or "8g",
            }

        services = [
            self._render_service(s, postgres_password=password) for s in self._services_for(spec)
        ]
        named_volumes = self._named_volumes_for(services)

        # Backwards-compatible companion input. Companions become ordinary
        # profile services at render time.
        companions: list[dict[str, object]] = [
            {
                "name": c.name,
                "image": None,
                "build_context": c.build_context,
                "dockerfile": c.dockerfile,
                "env_file": c.env_file,
                "environment": [
                    (k, self._expand_placeholders(v, postgres_password=password))
                    for k, v in c.environment
                ],
                "depends_on": list(c.depends_on),
                "healthcheck_cmd": c.healthcheck_cmd,
                "ports": list(c.ports),
                "command": c.command,
                "volumes": list(c.volumes),
                "privileged": False,
            }
            for c in spec.companions
        ]
        services.extend(companions)
        named_volumes = sorted({*named_volumes, *self._named_volumes_for(services)})

        # Agent waits for profile services that expose healthchecks. Services
        # without healthchecks may still run, but AWF cannot infer readiness.
        agent_depends_on = [s["name"] for s in services if s.get("healthcheck_cmd") is not None]

        agent_env = [
            (k, self._expand_placeholders(v, postgres_password=password))
            for k, v in spec.agent_environment
        ]
        if spec.docker_mode == "dind" and "DOCKER_HOST" not in {k for k, _ in agent_env}:
            agent_env.append(("DOCKER_HOST", "tcp://docker:2375"))

        rendered = self._env.get_template(self._template_name).render(
            workspace_id=spec.workspace_id,
            worktree_host_path=str(spec.worktree_host_path),
            agent_runtime_image=spec.agent_runtime_image,
            postgres_image=spec.postgres_image,
            postgres_user=spec.postgres_user,
            postgres_password=password,
            postgres_db=spec.postgres_db,
            resources=resources,
            auth_mounts=[
                {"source": m.source, "target": m.target, "mode": m.mode} for m in spec.auth_mounts
            ],
            git_name=spec.git_name,
            git_email=spec.git_email,
            agent_environment=agent_env,
            services=services,
            named_volumes=named_volumes,
            agent_depends_on=agent_depends_on,
            network_internal=spec.network_internal,
            host_gateway_enabled=spec.host_gateway_enabled,
        )
        compose_file.write_text(rendered, encoding="utf-8")

        return ComposeProjectPaths(project_dir=project_dir, compose_file=compose_file)

    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        """Start the stack. With ``wait=True``, blocks until services are healthy."""
        paths = self.render(spec)
        args = ["up", "-d", "--remove-orphans"]
        if wait:
            args.extend(["--wait", "--wait-timeout", str(spec.compose_up_timeout_seconds)])
        await self._compose(
            spec.project_name(),
            paths.compose_file,
            args,
            operation="up",
            capture_timeout_seconds=_compose_up_capture_timeout_seconds(
                spec.compose_up_timeout_seconds,
                wait=wait,
            ),
        )
        return paths

    async def ensure_project_up(
        self,
        *,
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        wait: bool = True,
        compose_up_timeout_seconds: int = 300,
    ) -> None:
        """Start an already-rendered compose project without re-rendering.

        Used by PR-monitor resume: the workspace row already records the
        compose project and compose.yml path that were active before the
        service restart. Re-rendering from a profile at resume time risks
        drifting from the stack the monitor originally owned.
        """
        args = ["up", "-d", "--remove-orphans"]
        if wait:
            args.extend(["--wait", "--wait-timeout", str(compose_up_timeout_seconds)])
        _log.info(
            "compose.ensure_project_up",
            workspace_id=workspace_id,
            project_name=project_name,
            compose_file=str(compose_file),
            wait=wait,
        )
        await self._compose(
            project_name,
            compose_file,
            args,
            operation="up",
            capture_timeout_seconds=_compose_up_capture_timeout_seconds(
                compose_up_timeout_seconds,
                wait=wait,
            ),
        )

    async def down(self, spec: WorkspaceComposeSpec, *, remove_volumes: bool = True) -> None:
        """Stop + remove the stack. Idempotent — absent projects are not errors."""
        paths = self._paths_for(spec)
        await self.down_project(
            project_name=spec.project_name(),
            compose_file=paths.compose_file,
            workspace_id=spec.workspace_id,
            remove_volumes=remove_volumes,
        )

    async def down_project(
        self,
        *,
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        remove_volumes: bool = True,
    ) -> None:
        """Stop + remove a stack using an already-rendered compose file path."""
        if not compose_file.exists():
            # Nothing rendered; assume never launched.
            _log.info("compose.down.noop", workspace_id=workspace_id)
            return

        args = ["down", "--remove-orphans"]
        if remove_volumes:
            args.append("-v")
        await self._compose(project_name, compose_file, args, operation="down")

    async def remove_project_by_label(
        self,
        *,
        project_name: str,
        workspace_id: str,
        remove_volumes: bool = True,
    ) -> None:
        """Best-effort removal when the compose file is unavailable."""
        label_filter = f"label=com.docker.compose.project={project_name}"
        container_ids = await self._docker_resource_ids(
            ["ps", "-aq", "--filter", label_filter],
            operation="ps",
        )
        if container_ids:
            await self._docker(["rm", "-f", *container_ids], operation="rm")

        network_ids = await self._docker_resource_ids(
            ["network", "ls", "-q", "--filter", label_filter],
            operation="network ls",
        )
        for network_id in network_ids:
            await self._docker(["network", "rm", network_id], operation="network rm")

        volume_names: list[str] = []
        if remove_volumes:
            volume_names = await self._docker_resource_ids(
                ["volume", "ls", "-q", "--filter", label_filter],
                operation="volume ls",
            )
            if volume_names:
                await self._docker(["volume", "rm", "-f", *volume_names], operation="volume rm")

        _log.info(
            "compose.project_label_removed",
            workspace_id=workspace_id,
            project_name=project_name,
            containers=len(container_ids),
            networks=len(network_ids),
            volumes=len(volume_names),
        )

    async def capture_companion_diagnostics(
        self,
        *,
        project_name: str,
        workspace_id: str,
        tail_lines: int = DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES,
    ) -> dict[str, Any]:
        """Capture redacted diagnostics for unhealthy companions in a project.

        Called on the service-startup failure path *before* the stack is torn
        down, so the failed companion containers and their logs still exist.
        The returned payload is **already redacted** (every captured string is
        passed through :func:`redact_audit_value`) and is safe to persist into a
        ``WorkspaceEvent``.

        This is **best-effort and never raises**: any docker error (daemon gone,
        container already removed, timeout, unparseable inspect output) is caught
        and recorded as a ``companion_logs_capture_error`` marker so the caller
        can still re-raise the original ``ComposeOperationError`` unchanged.

        The ``companion_logs_capture_error`` marker is always a uniform
        ``dict[str, str]`` so consumers never have to special-case its shape:
        top-level failures (``ps``/``inspect``/JSON) are keyed under
        ``"_top_level"`` and per-container ``docker logs`` failures under the
        compose service name.
        """
        compose_file = self._projects_dir / workspace_id / "compose.yml"
        payload: dict[str, Any] = {
            "schema": SERVICE_STARTUP_DIAGNOSTICS_SCHEMA,
            "compose_project": project_name,
            "compose_file": str(compose_file),
            "tail_lines": tail_lines,
            "containers_inspected": 0,
        }
        label_filter = f"label=com.docker.compose.project={project_name}"

        try:
            container_ids = await self._docker_resource_ids(
                ["ps", "-aq", "--filter", label_filter],
                operation="ps",
            )
        except ComposeOperationError as exc:
            return self._diagnostics_with_capture_error(payload, operation="ps", exc=exc)

        if not container_ids:
            return _redacted_diagnostics(payload)

        try:
            inspect_raw = await self._docker_capture(
                ["inspect", *container_ids],
                operation="inspect",
            )
            inspected = json.loads(inspect_raw)
        except ComposeOperationError as exc:
            return self._diagnostics_with_capture_error(payload, operation="inspect", exc=exc)
        except json.JSONDecodeError as exc:
            payload["companion_logs_capture_error"] = {
                "_top_level": f"inspect: unparseable JSON ({exc})"
            }
            return _redacted_diagnostics(payload)

        containers = inspected if isinstance(inspected, list) else []
        payload["containers_inspected"] = len(containers)

        companion_health: dict[str, Any] = {}
        healthcheck_test: dict[str, Any] = {}
        companion_logs: dict[str, list[str]] = {}
        logs_capture_error: dict[str, str] = {}

        for idx, container in enumerate(containers):
            if not _container_is_unhealthy(container):
                continue
            service = _compose_service_name(container) or str(
                container.get("Id") or f"<unknown-{idx}>"
            )
            companion_health[service] = _container_health_summary(container)
            test = _container_healthcheck_test(container)
            if test is not None:
                healthcheck_test[service] = test
            container_id = container.get("Id")
            if not container_id:
                continue
            try:
                logs_raw = await self._docker_capture(
                    ["logs", "--tail", str(tail_lines), str(container_id)],
                    operation="logs",
                    combine_stderr=True,
                )
            except ComposeOperationError as exc:
                logs_capture_error[service] = _capture_error_detail(exc)
                _log.warning(
                    "compose.companion_logs_capture_failed",
                    workspace_id=workspace_id,
                    service=service,
                    reason_code=exc.reason_code,
                    error=redact_secrets(str(exc)),
                )
                continue
            companion_logs[service] = logs_raw.splitlines()

        if companion_health:
            payload["companion_health"] = companion_health
        if healthcheck_test:
            payload["healthcheck_test"] = healthcheck_test
        if companion_logs:
            payload["companion_logs"] = companion_logs
        if logs_capture_error:
            payload["companion_logs_capture_error"] = logs_capture_error

        return _redacted_diagnostics(payload)

    def _diagnostics_with_capture_error(
        self,
        payload: dict[str, Any],
        *,
        operation: str,
        exc: ComposeOperationError,
    ) -> dict[str, Any]:
        """Attach a top-level capture-error marker and return a redacted payload."""
        _log.warning(
            "compose.companion_diagnostics_capture_failed",
            compose_project=payload.get("compose_project"),
            operation=operation,
            reason_code=exc.reason_code,
            error=redact_secrets(str(exc)),
        )
        payload["companion_logs_capture_error"] = {
            "_top_level": f"{operation}: {_capture_error_detail(exc)}"
        }
        return _redacted_diagnostics(payload)

    # ── Internals ──────────────────────────────────────────────────────────

    def _paths_for(self, spec: WorkspaceComposeSpec) -> ComposeProjectPaths:
        project_dir = self._projects_dir / spec.workspace_id
        return ComposeProjectPaths(
            project_dir=project_dir, compose_file=project_dir / "compose.yml"
        )

    async def _compose(
        self,
        project_name: str,
        compose_file: Path,
        args: list[str],
        *,
        operation: str,
        capture_timeout_seconds: float = COMPOSE_CAPTURE_TIMEOUT_SECONDS,
    ) -> None:
        cmd = [
            "docker",
            "compose",
            *args,
        ]
        env = {
            **os.environ,
            "COMPOSE_PROJECT_NAME": project_name,
            "COMPOSE_FILE": str(compose_file),
        }
        for attempt in range(2):
            _log.debug("compose.exec", operation=operation, cmd=cmd, attempt=attempt + 1)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=capture_timeout_seconds,
                )
            except FileNotFoundError as e:
                raise ComposeOperationError(
                    operation=operation,
                    returncode=127,
                    stdout="",
                    stderr=str(e),
                    reason_code="DOCKER_UNAVAILABLE",
                ) from e
            except TimeoutError as e:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(ProcessLookupError, TimeoutError):
                    await asyncio.wait_for(
                        proc.wait(),
                        timeout=_DOCKER_CAPTURE_KILL_WAIT_SECONDS,
                    )
                raise ComposeOperationError(
                    operation=operation,
                    returncode=124,
                    stdout="",
                    stderr=(
                        f"docker compose {operation} exceeded {capture_timeout_seconds:g}s timeout"
                    ),
                    reason_code="DOCKER_COMMAND_TIMEOUT",
                ) from e

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            assert proc.returncode is not None
            if proc.returncode == 0:
                return
            if attempt == 0 and _is_transient_compose_dispatch_failure(stderr):
                _log.warning(
                    "compose.dispatch_retry",
                    operation=operation,
                    stderr=stderr[:400],
                )
                continue
            reason_code = "COMPOSE_COMMAND_FAILED"
            err_lower = stderr.lower()
            if (
                "daemon" in err_lower
                or "error during connect" in err_lower
                or "docker endpoint" in err_lower
            ):
                reason_code = "DOCKER_UNAVAILABLE"

            raise ComposeOperationError(
                operation=operation,
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                reason_code=reason_code,
            )

    async def _docker_resource_ids(self, args: list[str], *, operation: str) -> list[str]:
        result = await self._docker_capture(args, operation=operation)
        return [line.strip() for line in result.splitlines() if line.strip()]

    async def _docker(self, args: list[str], *, operation: str) -> None:
        await self._docker_capture(args, operation=operation)

    async def _docker_capture(
        self, args: list[str], *, operation: str, combine_stderr: bool = False
    ) -> str:
        cmd = ["docker", *args]
        _log.debug("docker.exec", operation=operation, cmd=cmd)
        # ``docker logs`` writes a container's stderr to the CLI's stderr. The
        # default/primary AWF companion (postgres) logs entirely to stderr, so
        # diagnostics capture folds stderr into stdout to avoid empty logs on
        # exactly the failing-companion case we care about.
        stderr_target = asyncio.subprocess.STDOUT if combine_stderr else asyncio.subprocess.PIPE
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_target,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=DOCKER_CAPTURE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as e:
            raise ComposeOperationError(
                operation=operation,
                returncode=127,
                stdout="",
                stderr=str(e),
                reason_code="DOCKER_UNAVAILABLE",
            ) from e
        except TimeoutError as e:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(ProcessLookupError, TimeoutError):
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=_DOCKER_CAPTURE_KILL_WAIT_SECONDS,
                )
            raise ComposeOperationError(
                operation=operation,
                returncode=124,
                stdout="",
                stderr=(f"docker {operation} exceeded {DOCKER_CAPTURE_TIMEOUT_SECONDS:g}s timeout"),
                reason_code="DOCKER_COMMAND_TIMEOUT",
            ) from e

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        # With ``combine_stderr`` the child's stderr is folded into stdout, so
        # ``communicate()`` returns ``None`` for the stderr stream.
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes is not None else ""
        assert proc.returncode is not None
        if proc.returncode != 0:
            reason_code = "COMPOSE_COMMAND_FAILED"
            # With ``combine_stderr`` the child's stderr is folded into stdout, so a
            # daemon-unavailability message surfaces there; otherwise it is in stderr.
            err_lower = (stdout if combine_stderr else stderr).lower()
            if (
                "daemon" in err_lower
                or "error during connect" in err_lower
                or "docker endpoint" in err_lower
            ):
                reason_code = "DOCKER_UNAVAILABLE"
            raise ComposeOperationError(
                operation=operation,
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                reason_code=reason_code,
            )
        return stdout

    def _services_for(self, spec: WorkspaceComposeSpec) -> list[ComposeService]:
        services = list(spec.services)
        if spec.docker_mode == "dind":
            services.insert(
                0,
                ComposeService(
                    name="docker",
                    image=spec.dind_image,
                    environment=(("DOCKER_TLS_CERTDIR", ""),),
                    healthcheck_cmd="docker info >/dev/null 2>&1",
                    volumes=(("dind_data", "/var/lib/docker"),),
                    privileged=True,
                ),
            )
        return services

    def _render_service(
        self, service: ComposeService, *, postgres_password: str | None
    ) -> dict[str, object]:
        return {
            "name": service.name,
            "image": service.image,
            "build_context": service.build_context,
            "dockerfile": service.dockerfile,
            "env_file": service.env_file,
            "environment": [
                (k, self._expand_placeholders(v, postgres_password=postgres_password))
                for k, v in service.environment
            ],
            "depends_on": list(service.depends_on),
            "healthcheck_cmd": service.healthcheck_cmd,
            "ports": list(service.ports),
            "command": service.command,
            "volumes": list(service.volumes),
            "privileged": service.privileged,
        }

    def _named_volumes_for(self, services: list[dict[str, object]]) -> list[str]:
        names: set[str] = set()
        for service in services:
            volumes = service.get("volumes")
            if not isinstance(volumes, list):
                continue
            for item in volumes:
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                source = str(item[0])
                if source and "/" not in source and not source.startswith("."):
                    names.add(source)
        return sorted(names)

    def _expand_placeholders(self, value: str, *, postgres_password: str | None) -> str:
        if postgres_password is None:
            return value
        return value.replace("${AWF_POSTGRES_PASSWORD}", postgres_password)


def _redacted_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact every captured string in the diagnostics payload before persistence."""
    return cast("dict[str, Any]", redact_audit_value(payload))


def _capture_error_detail(exc: ComposeOperationError) -> str:
    """Summarize a docker capture failure for a diagnostics marker.

    WARNING: the returned string is UNREDACTED — it embeds ``exc.stderr``/
    ``exc.stdout``, which can contain credential material from docker output. It
    MUST pass through ``redact_audit_value`` (via ``_redacted_diagnostics``)
    before being persisted, returned to a caller, or logged directly. Every
    current caller stores it in a payload that is redacted unconditionally before
    return; new callers must preserve that contract.
    """
    detail = exc.stderr.strip() or exc.stdout.strip() or "<no output>"
    return f"{exc.reason_code}: {detail}"


def _container_is_unhealthy(container: Any) -> bool:
    """Return whether an inspected container is worth capturing diagnostics for.

    A container is interesting when its healthcheck is not ``healthy`` (failed,
    starting, or still probing) or — absent a healthcheck — when it has exited
    with a non-zero code.
    """
    if not isinstance(container, Mapping):
        return False
    state = container.get("State")
    if not isinstance(state, Mapping):
        return False
    health = state.get("Health")
    if isinstance(health, Mapping) and health.get("Status") is not None:
        return health.get("Status") != "healthy"
    exit_code = state.get("ExitCode") or 0
    return state.get("Status") == "exited" and exit_code != 0


def _compose_service_name(container: Mapping[str, Any]) -> str | None:
    """Return the compose service label for a container, if present."""
    config = container.get("Config")
    labels = config.get("Labels") if isinstance(config, Mapping) else None
    if not isinstance(labels, Mapping):
        return None
    name = labels.get("com.docker.compose.service")
    return name if isinstance(name, str) and name else None


def _container_health_summary(container: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize state + the trailing ``.State.Health.Log`` entries for a container."""
    state = container.get("State")
    state_map = state if isinstance(state, Mapping) else {}
    health = state_map.get("Health")
    health_map = health if isinstance(health, Mapping) else {}
    raw_log = health_map.get("Log")
    health_log: list[dict[str, Any]] = []
    if isinstance(raw_log, list):
        for entry in raw_log[-_SERVICE_STARTUP_HEALTH_LOG_TAIL_ENTRIES:]:
            if not isinstance(entry, Mapping):
                continue
            health_log.append({"ExitCode": entry.get("ExitCode"), "Output": entry.get("Output")})
    return {
        "status": state_map.get("Status"),
        "exit_code": state_map.get("ExitCode"),
        "health_status": health_map.get("Status"),
        "health_log": health_log,
    }


def _container_healthcheck_test(container: Mapping[str, Any]) -> list[Any] | None:
    """Return the rendered healthcheck ``Test`` array as parsed by compose, if any."""
    config = container.get("Config")
    healthcheck = config.get("Healthcheck") if isinstance(config, Mapping) else None
    test = healthcheck.get("Test") if isinstance(healthcheck, Mapping) else None
    return test if isinstance(test, list) else None
