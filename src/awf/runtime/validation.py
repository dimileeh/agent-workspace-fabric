"""Validation runner — executes test commands inside the workspace container.

Contract:
    1. If ``requires_database`` is True, run ``alembic upgrade head`` AFTER
       the first test command (which is the convention for dep-install).
       This is deliberate: dep install must happen first so Alembic + the
       app package are importable. If the app doesn't need this pattern,
       set ``requires_database=False`` and put migration in test_commands
       yourself.
    2. Run each command in ``test_commands`` sequentially via ``docker
       compose exec -T -w /workspace agent sh -lc <command>``.
    3. Capture stdout + stderr for each command to per-workspace artifact
       files so operators can read them after the run.
    4. Returns a structured ValidationResult — never raises on test failure
       (the provisioner maps the result to a workspace state transition).

We route through ``AsyncCommandRunner`` so tests inject FakeCommandRunner
and don't require a real docker daemon.

Migration ordering rationale:
    The naive ordering "migration first, then tests" fails for any repo
    whose migration runner depends on installed Python deps (Alembic +
    the app package). Running migration AFTER the first test command
    (which is by convention ``uv pip install -e ".[dev]"`` or similar)
    sidesteps that dependency chain without requiring every caller to
    duplicate the migration line in their test_commands.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.logging import get_logger

_log = get_logger(__name__)

# Every validation command runs through this preamble so a workspace-local
# ``.venv`` (typical for ``uv pip install``) is picked up automatically.
# Without this, ``uv pip install`` creates ``/workspace/.venv`` but subsequent
# commands invoke ``/usr/local/bin/alembic`` / ``pytest`` which don't see the
# venv's site-packages — classic ``ModuleNotFoundError: No module named '<app>'``.
_VENV_ACTIVATE_PREAMBLE = (
    "[ -f /workspace/.venv/bin/activate ] && . /workspace/.venv/bin/activate; "
)

_MIGRATION_SHELL = _VENV_ACTIVATE_PREAMBLE + "alembic upgrade head"


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
        workspace_worktree: Path | None = None,
    ) -> ValidationResult:
        workspace_artifacts = self._artifacts_dir / workspace_id
        workspace_artifacts.mkdir(parents=True, exist_ok=True)

        migration: ValidationCommandResult | None = None
        cmd_results: list[ValidationCommandResult] = []

        for index, raw in enumerate(test_commands, start=1):
            # Commands are full shell strings (e.g. ``pytest -q``); we invoke
            # them under ``sh -lc`` so quoting, pipes, and env var expansion
            # inside the container all work as the operator expects. The
            # venv-activate preamble makes ``uv pip install``-created venvs
            # visible to every subsequent tool (alembic, pytest, ruff, ...).
            label = f"cmd_{index:02d}"
            result = await self._exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=["sh", "-lc", _VENV_ACTIVATE_PREAMBLE + raw],
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
                return ValidationResult(migration=migration, commands=cmd_results)

            # Migration runs AFTER the first successful command (typically a
            # dep-install) so Alembic + the app package are importable. If the
            # migration fails, skip remaining test commands: they'd run against
            # a broken schema and the output would be noise.
            if index == 1 and requires_database:
                # ``requires_database=True`` means "the companion stack needs
                # Postgres running" — not "run Alembic here." Non-Python
                # workspaces (aira-web, etc.) have no ``alembic.ini`` at the
                # worktree root; their companion backend handles its own
                # migrations via its own entrypoint. Skip the workspace-side
                # step for them rather than burn fix-passes on a shell error.
                # When ``workspace_worktree`` is not provided (e.g. legacy
                # unit tests that assert the historical migration behavior)
                # we fall back to the old "just run it" path.
                if (
                    workspace_worktree is not None
                    and not (workspace_worktree / "alembic.ini").is_file()
                ):
                    _log.info(
                        "validation.migration_skipped_no_alembic_ini",
                        workspace_id=workspace_id,
                        reason=(
                            "requires_database=True but workspace has no "
                            "alembic.ini — skipping migration step"
                        ),
                    )
                    continue
                migration = await self._exec(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    cli_args=["sh", "-lc", _MIGRATION_SHELL],
                    label="migration",
                    artifacts_dir=workspace_artifacts,
                )
                if not migration.ok:
                    _log.warning(
                        "validation.migration_failed",
                        workspace_id=workspace_id,
                        returncode=migration.returncode,
                    )
                    return ValidationResult(migration=migration, commands=cmd_results)

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

        if len(cli_args) == 3 and cli_args[0] == "sh":
            # Strip the internal venv-activate preamble so the display is the
            # user-supplied command they care about, not our plumbing.
            shell_cmd = cli_args[2]
            if shell_cmd.startswith(_VENV_ACTIVATE_PREAMBLE):
                shell_cmd = shell_cmd[len(_VENV_ACTIVATE_PREAMBLE) :]
            display = shell_cmd
        else:
            display = " ".join(shlex.quote(a) for a in cli_args)
        return ValidationCommandResult(
            command=display,
            returncode=result.returncode,
            duration_seconds=duration,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
