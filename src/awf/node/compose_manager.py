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
import secrets
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from awf.common.logging import get_logger

_log = get_logger(__name__)


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


@dataclass(frozen=True)
class WorkspaceComposeSpec:
    """Everything the template needs to render one workspace stack."""

    workspace_id: str
    worktree_host_path: Path
    agent_runtime_image: str = "awf-agent-runtime:latest"
    postgres_image: str = "postgres:16-alpine"
    postgres_user: str = "awf"
    postgres_db: str = "awf"
    postgres_password: str | None = None  # None → generate one
    cpu_limit: str | None = None
    memory_limit: str | None = None
    auth_mounts: tuple[AuthMount, ...] = ()
    git_name: str | None = None
    git_email: str | None = None
    companions: tuple[CompanionService, ...] = ()

    def project_name(self) -> str:
        return f"awf_{self.workspace_id}"


@dataclass(frozen=True)
class ComposeProjectPaths:
    project_dir: Path
    compose_file: Path


class ComposeManager:
    """Renders, launches, and tears down per-workspace compose stacks."""

    def __init__(self, *, work_dir: Path, template_path: Path) -> None:
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

        companions = [
            {
                "name": c.name,
                "build_context": c.build_context,
                "dockerfile": c.dockerfile,
                "env_file": c.env_file,
                "environment": list(c.environment),
                "depends_on": list(c.depends_on),
                "healthcheck_cmd": c.healthcheck_cmd,
                "ports": list(c.ports),
                "command": c.command,
                "volumes": list(c.volumes),
            }
            for c in spec.companions
        ]
        # Agent waits for postgres always, plus any companion that has a healthcheck
        # (otherwise ``service_started`` would race the companion into existence
        # but not readiness).
        agent_depends_on = ["postgres"] + [
            c.name for c in spec.companions if c.healthcheck_cmd is not None
        ]

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
            companions=companions,
            agent_depends_on=agent_depends_on,
        )
        compose_file.write_text(rendered, encoding="utf-8")

        return ComposeProjectPaths(project_dir=project_dir, compose_file=compose_file)

    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        """Start the stack. With ``wait=True``, blocks until services are healthy."""
        paths = self.render(spec)
        args = ["up", "-d"]
        if wait:
            args.append("--wait")
        await self._compose(spec.project_name(), paths.compose_file, args, operation="up")
        return paths

    async def down(self, spec: WorkspaceComposeSpec, *, remove_volumes: bool = True) -> None:
        """Stop + remove the stack. Idempotent — absent projects are not errors."""
        paths = self._paths_for(spec)
        if not paths.compose_file.exists():
            # Nothing rendered; assume never launched.
            _log.info("compose.down.noop", workspace_id=spec.workspace_id)
            return

        args = ["down"]
        if remove_volumes:
            args.append("-v")
        await self._compose(spec.project_name(), paths.compose_file, args, operation="down")

    # ── Internals ──────────────────────────────────────────────────────────

    def _paths_for(self, spec: WorkspaceComposeSpec) -> ComposeProjectPaths:
        project_dir = self._projects_dir / spec.workspace_id
        return ComposeProjectPaths(
            project_dir=project_dir, compose_file=project_dir / "compose.yml"
        )

    async def _compose(
        self, project_name: str, compose_file: Path, args: list[str], *, operation: str
    ) -> None:
        cmd = [
            "docker",
            "compose",
            "--project-name",
            project_name,
            "--file",
            str(compose_file),
            *args,
        ]
        _log.debug("compose.exec", operation=operation, cmd=cmd)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        assert proc.returncode is not None
        if proc.returncode != 0:
            raise ComposeOperationError(
                operation=operation,
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )
