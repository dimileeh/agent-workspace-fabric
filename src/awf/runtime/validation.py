"""Profile-driven phase runner.

The old AWF validation path treated validation as "test commands plus an
optional Alembic migration." The universal model is simpler and more honest:
projects declare lifecycle phases in a workspace profile, and AWF executes
those commands without knowing whether the project is Python, Node, Go, Java,
C++, or Docker Compose.

``ValidationRunner.run(...)`` remains as a small compatibility wrapper for old
callers that pass raw command strings. New code should call
``run_profile_phases(...)`` with a resolved ``WorkspaceProfile``.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.logging import get_logger
from awf.profiles.models import ProfileCommand, ProfileCoverage, WorkspaceProfile
from awf.runtime.logs import LogStore

_log = get_logger(__name__)

# Every command runs through this preamble so a workspace-local ``.venv``
# created during setup is picked up automatically by later Python commands.
_VENV_ACTIVATE_PREAMBLE = (
    "[ -f /workspace/.venv/bin/activate ] && . /workspace/.venv/bin/activate; "
)
_COVERAGE_TOTAL_RE = re.compile(r"(?im)^\s*TOTAL\b.*?(?P<percent>\d+(?:\.\d+)?)%\s*$")
_COVERAGE_SUMMARY_RE = re.compile(
    r"(?i)\b(?:total\s+coverage|coverage)\D+(?P<percent>\d+(?:\.\d+)?)%"
)


@dataclass(frozen=True)
class ValidationCommandResult:
    """One phase command's outcome + captured artifact paths."""

    command: str
    returncode: int
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    phase: str = "validate"
    reason_code: str = "COMMAND_FAILED"
    stream_ids: dict[str, str | None] = field(default_factory=dict)
    retry_count: int = 0
    policy_failed: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.policy_failed


@dataclass(frozen=True)
class ValidationCoverageResult:
    """Coverage measurement parsed from an explicit provider command."""

    provider: str
    percent: float | None
    minimum_percent: float
    enforce: bool
    status: str
    reason_code: str
    command_result: ValidationCommandResult | None = None

    @property
    def ok(self) -> bool:
        return self.status != "failed"

    def as_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "provider": self.provider,
            "minimum_percent": float(self.minimum_percent),
            "enforce": self.enforce,
            "status": self.status,
            "reason_code": self.reason_code,
        }
        if self.percent is not None:
            metadata["percent"] = float(self.percent)
        return metadata


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a profile phase sequence."""

    migration: ValidationCommandResult | None = None
    commands: list[ValidationCommandResult] = field(default_factory=list)
    coverage: ValidationCoverageResult | None = None

    @property
    def all_passed(self) -> bool:
        if self.migration is not None and not self.migration.ok:
            return False
        if self.coverage is not None and not self.coverage.ok:
            return False
        return all(c.ok for c in self.commands)

    @property
    def total_retries(self) -> int:
        return sum(c.retry_count for c in self.commands)

    @property
    def first_failure(self) -> ValidationCommandResult | None:
        if self.migration is not None and not self.migration.ok:
            return self.migration
        first_command_failure = next((c for c in self.commands if not c.ok), None)
        if first_command_failure is not None:
            return first_command_failure
        if self.coverage is not None and not self.coverage.ok:
            return self.coverage.command_result
        return None


class ValidationRunner:
    """Runs profile phases inside the per-workspace agent container."""

    def __init__(
        self,
        *,
        runner: AsyncCommandRunner,
        artifacts_dir: Path,
        log_store: LogStore | None = None,
    ) -> None:
        self._runner = runner
        self._artifacts_dir = artifacts_dir
        self._log_store = log_store

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
        """Legacy compatibility wrapper.

        ``requires_database`` and ``workspace_worktree`` are accepted for API
        stability but no longer trigger implicit Alembic behavior. Database
        migrations belong in a profile phase.
        """
        if requires_database:
            _log.info(
                "validation.requires_database_ignored",
                workspace_id=workspace_id,
                reason="database setup is profile-driven in the universal runner",
            )
        del workspace_worktree
        commands = [("validate", ProfileCommand(command=c)) for c in test_commands]
        return await self._run_commands(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            commands=commands,
            healthchecks=[],
            legacy_command_labels=True,
            coverage=None,
        )

    async def run_profile_phases(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
        phase_names: list[str] | tuple[str, ...],
        run_healthchecks: bool = False,
    ) -> ValidationResult:
        """Run the selected profile phases in order."""
        healthchecks = profile.validation.healthchecks if run_healthchecks else []
        coverage = profile.validation.coverage if "validate" in set(phase_names) else None
        return await self._run_commands(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            commands=profile.phases.commands_for(phase_names),
            healthchecks=[
                (
                    "healthcheck",
                    ProfileCommand(command=h.command, timeout_seconds=h.timeout_seconds),
                )
                for h in healthchecks
            ],
            legacy_command_labels=False,
            retry_budget=profile.validation.retry_budget,
            coverage=coverage,
        )

    async def _run_commands(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        commands: list[tuple[str, ProfileCommand]],
        healthchecks: list[tuple[str, ProfileCommand]],
        legacy_command_labels: bool,
        retry_budget: int = 0,
        coverage: ProfileCoverage | None,
    ) -> ValidationResult:
        workspace_artifacts = self._artifacts_dir / workspace_id
        workspace_artifacts.mkdir(parents=True, exist_ok=True)

        results: list[ValidationCommandResult] = []
        ordered = [*healthchecks, *commands]
        phase_indices: dict[str, int] = {}
        for index, (phase, command) in enumerate(ordered, start=1):
            if legacy_command_labels:
                label = f"cmd_{index:02d}"
            else:
                phase_indices[phase] = phase_indices.get(phase, 0) + 1
                label = f"{phase_indices[phase]:02d}_{phase}"

            attempts = 0
            while True:
                result = await self._exec(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    cli_args=["sh", "-lc", _VENV_ACTIVATE_PREAMBLE + command.command],
                    label=label,
                    artifacts_dir=workspace_artifacts,
                    phase=phase,
                    timeout_seconds=command.timeout_seconds,
                    is_retry=(attempts > 0),
                )

                if result.ok or not command.required:
                    results.append(
                        ValidationCommandResult(
                            command=result.command,
                            returncode=result.returncode,
                            duration_seconds=result.duration_seconds,
                            stdout_path=result.stdout_path,
                            stderr_path=result.stderr_path,
                            phase=result.phase,
                            reason_code=result.reason_code,
                            stream_ids=result.stream_ids,
                            retry_count=attempts,
                        )
                    )
                    break

                # 124: command timeout. > 128: killed by signal (e.g., 137 OOM kill).
                # We treat most signal exits as potentially flaky infrastructure events,
                # accepting the trade-off that deterministic failures like SIGILL or SIGABRT
                # might be needlessly retried.
                is_flaky = result.returncode == 124 or result.returncode > 128
                if is_flaky and attempts < retry_budget:
                    attempts += 1
                    _log.info(
                        "validation.phase_command_flaky_retry",
                        workspace_id=workspace_id,
                        phase=phase,
                        command=command.command,
                        returncode=result.returncode,
                        attempt=attempts,
                        budget=retry_budget,
                    )
                    continue

                if is_flaky and attempts >= retry_budget and retry_budget > 0:
                    result = ValidationCommandResult(
                        command=result.command,
                        returncode=result.returncode,
                        duration_seconds=result.duration_seconds,
                        stdout_path=result.stdout_path,
                        stderr_path=result.stderr_path,
                        phase=result.phase,
                        reason_code="VALIDATION_RETRY_EXHAUSTED",
                        stream_ids=result.stream_ids,
                        retry_count=attempts,
                    )
                else:
                    result = ValidationCommandResult(
                        command=result.command,
                        returncode=result.returncode,
                        duration_seconds=result.duration_seconds,
                        stdout_path=result.stdout_path,
                        stderr_path=result.stderr_path,
                        phase=result.phase,
                        reason_code=result.reason_code,
                        stream_ids=result.stream_ids,
                        retry_count=attempts,
                    )

                results.append(result)
                _log.info(
                    "validation.phase_command_failed",
                    workspace_id=workspace_id,
                    phase=phase,
                    command=command.command,
                    returncode=result.returncode,
                    reason_code=result.reason_code,
                )
                return ValidationResult(commands=results)

        coverage_result: ValidationCoverageResult | None = None
        if coverage is not None and _coverage_requested(coverage):
            coverage_result = await self._collect_coverage(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                coverage=coverage,
                artifacts_dir=workspace_artifacts,
                results=results,
                phase_indices=phase_indices,
            )

        return ValidationResult(commands=results, coverage=coverage_result)

    async def _collect_coverage(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        coverage: ProfileCoverage,
        artifacts_dir: Path,
        results: list[ValidationCommandResult],
        phase_indices: dict[str, int],
    ) -> ValidationCoverageResult:
        if coverage.provider != "python":
            return ValidationCoverageResult(
                provider=coverage.provider,
                percent=None,
                minimum_percent=coverage.minimum_percent,
                enforce=coverage.enforce,
                status="failed" if coverage.enforce else "unsupported",
                reason_code="COVERAGE_PROVIDER_UNSUPPORTED",
            )

        command_result: ValidationCommandResult | None = None
        if coverage.command is not None:
            phase_indices["coverage"] = phase_indices.get("coverage", 0) + 1
            label = f"{phase_indices['coverage']:02d}_coverage"
            command_result = await self._exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=["sh", "-lc", _VENV_ACTIVATE_PREAMBLE + coverage.command.command],
                label=label,
                artifacts_dir=artifacts_dir,
                phase="coverage",
                timeout_seconds=coverage.command.timeout_seconds,
            )
            coverage_outputs = [command_result]
        else:
            coverage_outputs = results

        percent = _parse_python_coverage_percent_from_files(
            _coverage_output_paths(coverage_outputs)
        )
        reason_code = _coverage_reason_code(
            percent=percent,
            minimum_percent=coverage.minimum_percent,
            command_result=command_result,
        )
        status = _coverage_status(reason_code=reason_code, enforce=coverage.enforce)
        policy_failed = status == "failed"
        if command_result is not None:
            command_result = replace(
                command_result,
                reason_code=reason_code,
                policy_failed=policy_failed,
            )
            results.append(command_result)

        _log.info(
            "validation.coverage_collected",
            workspace_id=workspace_id,
            provider=coverage.provider,
            percent=percent,
            minimum_percent=coverage.minimum_percent,
            enforce=coverage.enforce,
            reason_code=reason_code,
            status=status,
        )
        return ValidationCoverageResult(
            provider=coverage.provider,
            percent=percent,
            minimum_percent=coverage.minimum_percent,
            enforce=coverage.enforce,
            status=status,
            reason_code=reason_code,
            command_result=command_result,
        )

    async def _exec(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        cli_args: list[str],
        label: str,
        artifacts_dir: Path,
        phase: str = "validate",
        timeout_seconds: int | None = None,
        is_retry: bool = False,
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
        reason_code = "COMMAND_FAILED"
        base_stream_id = f"validation.{label}"
        stream_ids: dict[str, str | None] = {
            "stdout": f"{base_stream_id}.stdout",
            "stderr": f"{base_stream_id}.stderr",
        }
        sinks = None
        if self._log_store is not None:
            sinks = await self._log_store.open_command_streams(
                workspace_id=artifacts_dir.name,
                base_stream_id=base_stream_id,
                source="validation",
                name=f"{phase} {label}",
            )
        try:
            run_streaming = getattr(self._runner, "run_streaming", None)
            if timeout_seconds is None:
                if sinks is not None and run_streaming is not None:
                    result: CommandResult = await run_streaming(
                        docker_args,
                        on_stdout=sinks.write_stdout,
                        on_stderr=sinks.write_stderr,
                    )
                else:
                    result = await self._runner.run(docker_args)
                    if sinks is not None:
                        await sinks.write_stdout(result.stdout)
                        await sinks.write_stderr(result.stderr)
            else:
                if sinks is not None and run_streaming is not None:
                    result = await asyncio.wait_for(
                        run_streaming(
                            docker_args,
                            on_stdout=sinks.write_stdout,
                            on_stderr=sinks.write_stderr,
                        ),
                        timeout=timeout_seconds,
                    )
                else:
                    result = await asyncio.wait_for(
                        self._runner.run(docker_args),
                        timeout=timeout_seconds,
                    )
                    if sinks is not None:
                        await sinks.write_stdout(result.stdout)
                        await sinks.write_stderr(result.stderr)
        except TimeoutError:
            result = CommandResult(
                returncode=124,
                stdout="",
                stderr=f"command timed out after {timeout_seconds}s",
            )
            reason_code = "PHASE_TIMEOUT"
            if sinks is not None:
                await sinks.write_stderr(result.stderr)
        finally:
            if sinks is not None:
                await sinks.close()
        duration = time.monotonic() - started

        stdout_path = artifacts_dir / f"{label}.stdout"
        stderr_path = artifacts_dir / f"{label}.stderr"
        mode = "a" if is_retry else "w"
        with stdout_path.open(mode, encoding="utf-8") as f:
            f.write(result.stdout)
        with stderr_path.open(mode, encoding="utf-8") as f:
            f.write(result.stderr)

        display = _display_command(cli_args)
        return ValidationCommandResult(
            command=display,
            returncode=result.returncode,
            duration_seconds=duration,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            phase=phase,
            reason_code=reason_code,
            stream_ids=stream_ids,
        )


def _display_command(cli_args: list[str]) -> str:
    if len(cli_args) == 3 and cli_args[0] == "sh":
        shell_cmd = cli_args[2]
        if shell_cmd.startswith(_VENV_ACTIVATE_PREAMBLE):
            shell_cmd = shell_cmd[len(_VENV_ACTIVATE_PREAMBLE) :]
        return shell_cmd
    return " ".join(shlex.quote(a) for a in cli_args)



def _coverage_requested(coverage: ProfileCoverage) -> bool:
    return coverage.command is not None or coverage.minimum_percent > 0


def _coverage_output_paths(results: list[ValidationCommandResult]) -> list[Path]:
    paths: list[Path] = []
    for result in results:
        paths.extend((result.stdout_path, result.stderr_path))
    return paths


def _parse_python_coverage_percent_from_files(paths: list[Path]) -> float | None:
    total_percent: float | None = None
    summary_percent: float | None = None
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                total_match = _COVERAGE_TOTAL_RE.search(line)
                if total_match:
                    total_percent = float(total_match.group("percent"))
                    continue
                summary_match = _COVERAGE_SUMMARY_RE.search(line)
                if summary_match:
                    summary_percent = float(summary_match.group("percent"))

    return total_percent if total_percent is not None else summary_percent


def _coverage_reason_code(
    *,
    percent: float | None,
    minimum_percent: float,
    command_result: ValidationCommandResult | None,
) -> str:
    if percent is None:
        if command_result is not None and command_result.returncode != 0:
            return "COVERAGE_COMMAND_FAILED"
        return "COVERAGE_NOT_FOUND"
    if percent < minimum_percent:
        return "COVERAGE_BELOW_THRESHOLD"
    if command_result is not None and command_result.returncode != 0:
        return "COVERAGE_COMMAND_FAILED"
    return "COVERAGE_OK"


def _coverage_status(*, reason_code: str, enforce: bool) -> str:
    if reason_code == "COVERAGE_OK":
        return "passed"
    if enforce:
        return "failed"
    return "reported"
