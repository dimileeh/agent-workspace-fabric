"""Validation runner — executes test commands inside the workspace container.

Contract for Task 6:
    1. If ``requires_database``, run ``alembic upgrade head`` inside the
       agent container first. If it fails, ValidationResult.all_passed is
       False and the individual test commands do NOT run.
    2. Run each command in ``test_commands`` sequentially via ``docker
       compose exec -T -w /workspace agent sh -lc <command>``.
    3. Capture stdout + stderr for each command to per-workspace artifact
       files so operators can read them after the run.
    4. Returns a structured ValidationResult — never raises on test failure
       (the provisioner maps the result to a workspace state transition).

We route through ``AsyncCommandRunner`` so tests inject FakeCommandRunner
and don't require a real docker daemon.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.logging import get_logger

_log = get_logger(__name__)

_MIGRATION_COMMAND: tuple[str, ...] = ("alembic", "upgrade", "head")


@dataclass(frozen=True)
class ValidationCommandResult:
    """One command's outcome + paths to its captured stdout/stderr."""

    command: str
    returncode: int
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of the full validation sequence for one workspace."""

    migration: ValidationCommandResult | None = None
    commands: list[ValidationCommandResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        if self.migration is not None and not self.migration.ok:
            return False
        return all(c.ok for c in self.commands)

    @property
    def first_failure(self) -> ValidationCommandResult | None:
        if self.migration is not None and not self.migration.ok:
            return self.migration
        return next((c for c in self.commands if not c.ok), None)


class ValidationRunner:
    """Runs validation commands inside the per-workspace agent container.

    Artifacts are written under ``<artifacts_dir>/<workspace_id>/``:

        migration.stdout / migration.stderr    (only if requires_database)
        cmd_01.stdout / cmd_01.stderr          (for the first test_command)
        cmd_02.stdout / cmd_02.stderr          ...
    """

    def __init__(self, *, runner: AsyncCommandRunner, artifacts_dir: Path) -> None:
        self._runner = runner
        self._artifacts_dir = artifacts_dir

    async def run(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        test_commands: list[str],
        requires_database: bool = False,
    ) -> ValidationResult:
        workspace_artifacts = self._artifacts_dir / workspace_id
        workspace_artifacts.mkdir(parents=True, exist_ok=True)

        migration: ValidationCommandResult | None = None
        if requires_database:
            migration = await self._exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=list(_MIGRATION_COMMAND),
                label="migration",
                artifacts_dir=workspace_artifacts,
            )
            if not migration.ok:
                _log.warning(
                    "validation.migration_failed",
                    workspace_id=workspace_id,
                    returncode=migration.returncode,
                )
                # Short-circuit: running test commands against a broken schema
                # produces misleading failures. Let the caller see the migration
                # error as the first/only failure.
                return ValidationResult(migration=migration, commands=[])

        cmd_results: list[ValidationCommandResult] = []
        for index, raw in enumerate(test_commands, start=1):
            # Commands are full shell strings (e.g. ``pytest -q``); we invoke
            # them under ``sh -lc`` so quoting, pipes, and env var expansion
            # inside the container all work as the operator expects.
            label = f"cmd_{index:02d}"
            result = await self._exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=["sh", "-lc", raw],
                label=label,
                artifacts_dir=workspace_artifacts,
            )
            cmd_results.append(result)
            if not result.ok:
                _log.info(
                    "validation.command_failed",
                    workspace_id=workspace_id,
                    command=raw,
                    returncode=result.returncode,
                )
                # Stop at first failure — there's no point running later
                # commands when an earlier one (e.g. lint) failed.
                break

        return ValidationResult(migration=migration, commands=cmd_results)

    async def _exec(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        cli_args: list[str],
        label: str,
        artifacts_dir: Path,
    ) -> ValidationCommandResult:
        docker_args = [
            "docker",
            "compose",
            "--project-name",
            compose_project,
            "--file",
            str(compose_file),
            "exec",
            "-T",
            "-w",
            "/workspace",
            "agent",
            *cli_args,
        ]
        started = time.monotonic()
        result: CommandResult = await self._runner.run(docker_args)
        duration = time.monotonic() - started

        stdout_path = artifacts_dir / f"{label}.stdout"
        stderr_path = artifacts_dir / f"{label}.stderr"
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")

        display = (
            " ".join(shlex.quote(a) for a in cli_args)
            if len(cli_args) != 3 or cli_args[0] != "sh"
            else cli_args[2]  # raw user command for sh -lc
        )
        return ValidationCommandResult(
            command=display,
            returncode=result.returncode,
            duration_seconds=duration,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
