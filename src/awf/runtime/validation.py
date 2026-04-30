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
import json
import re
import shlex
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    AsyncCommandRunner,
    CommandResult,
)
from awf.common.compose_exec import (
    build_tracked_compose_exec,
    cleanup_compose_exec_invocation,
    cleanup_compose_exec_invocation_after_cancellation,
)
from awf.common.logging import get_logger
from awf.profiles.models import (
    ProfileAlembicValidation,
    ProfileCommand,
    ProfileCoverage,
    ProfileHealthCheck,
    WorkspaceProfile,
)
from awf.runtime.alembic_validation import (
    ALEMBIC_MIGRATION_POLICY_COMMAND,
    ALEMBIC_MIGRATION_POLICY_PHASE,
    alembic_policy_metadata,
    validate_alembic_migration_chain,
)
from awf.runtime.logs import LogStore
from awf.service.alembic_resolver import AlembicGraphValidationStatus

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
_COVERAGE_FILE_LINE_RE = re.compile(
    r"^(?P<file>\S.*?)\s+(?P<stmts>\d+)\s+(?P<miss>\d+)\s+(?P<cover>\d+)%\s*(?P<missing>.*?)\s*$"
)
_COVERAGE_HEADER_RE = re.compile(r"(?i)Name\s+Stmts\s+Miss\s+Cover")
HEALTHCHECK_OK = "HEALTHCHECK_OK"
HEALTHCHECK_TIMEOUT = "HEALTHCHECK_TIMEOUT"
HEALTHCHECK_COMMAND_FAILED = "HEALTHCHECK_COMMAND_FAILED"
HEALTHCHECK_HTTP_STATUS_MISMATCH = "HEALTHCHECK_HTTP_STATUS_MISMATCH"
HEALTHCHECK_INVALID_CONFIGURATION = "HEALTHCHECK_INVALID_CONFIGURATION"

_HTTP_HEALTHCHECK_SCRIPT = (
    "import sys, urllib.error, urllib.request\n"
    "method, url, expected_raw, timeout_raw = sys.argv[1:5]\n"
    "expected = int(expected_raw)\n"
    "timeout = float(timeout_raw)\n"
    "request = urllib.request.Request(url, method=method)\n"
    "try:\n"
    "    with urllib.request.urlopen(request, timeout=timeout) as response:\n"
    "        status = response.getcode()\n"
    "except urllib.error.HTTPError as exc:\n"
    "    status = exc.code\n"
    "except Exception as exc:\n"
    "    print(f'healthcheck request failed: {type(exc).__name__}: {exc}', file=sys.stderr)\n"
    "    sys.exit(2)\n"
    "print(f'http status {status} expected {expected}')\n"
    "sys.exit(0 if status == expected else 1)\n"
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
    metadata: dict[str, object] = field(default_factory=dict)

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
    gaps: list[dict[str, object]] = field(default_factory=list)

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
        if self.gaps:
            metadata["gaps"] = self.gaps
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
        worktree_path: Path | None = None,
    ) -> ValidationResult:
        """Run the selected profile phases in order."""
        requested_phases = set(phase_names)
        healthchecks = profile.validation.healthchecks if run_healthchecks else []
        coverage = profile.validation.coverage if "validate" in requested_phases else None
        alembic_policy = (
            profile.validation.alembic if "validate" in requested_phases else None
        )
        return await self._run_commands(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            commands=profile.phases.commands_for(phase_names),
            healthchecks=list(healthchecks),
            legacy_command_labels=False,
            retry_budget=profile.validation.retry_budget,
            coverage=coverage,
            alembic_policy=alembic_policy,
            worktree_path=worktree_path,
        )

    async def run_profile_coverage(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
        phase: str = "coverage",
    ) -> ValidationCoverageResult | None:
        """Run only the profile coverage command and return its policy result."""
        coverage = profile.validation.coverage
        if not _coverage_requested(coverage):
            return None
        workspace_artifacts = self._artifacts_dir / workspace_id
        workspace_artifacts.mkdir(parents=True, exist_ok=True)
        return await self._collect_coverage(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            coverage=coverage,
            artifacts_dir=workspace_artifacts,
            results=[],
            phase_indices={},
            phase=phase,
        )

    async def _run_commands(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        commands: list[tuple[str, ProfileCommand]],
        healthchecks: list[ProfileHealthCheck],
        legacy_command_labels: bool,
        retry_budget: int = 0,
        coverage: ProfileCoverage | None,
        alembic_policy: ProfileAlembicValidation | None = None,
        worktree_path: Path | None = None,
    ) -> ValidationResult:
        workspace_artifacts = self._artifacts_dir / workspace_id
        workspace_artifacts.mkdir(parents=True, exist_ok=True)

        results: list[ValidationCommandResult] = []
        phase_indices: dict[str, int] = {}
        if alembic_policy is not None and alembic_policy.enabled:
            phase = ALEMBIC_MIGRATION_POLICY_PHASE
            phase_indices[phase] = phase_indices.get(phase, 0) + 1
            label = f"{phase_indices[phase]:02d}_{phase}"
            result = await self._run_alembic_policy(
                workspace_id=workspace_id,
                policy=alembic_policy,
                worktree_path=worktree_path,
                label=label,
                artifacts_dir=workspace_artifacts,
            )
            results.append(result)
            if not result.ok:
                _log.info(
                    "validation.alembic_migration_policy_failed",
                    workspace_id=workspace_id,
                    reason_code=result.reason_code,
                    returncode=result.returncode,
                )
                return ValidationResult(commands=results)

        for healthcheck in healthchecks:
            phase = "healthcheck"
            phase_indices[phase] = phase_indices.get(phase, 0) + 1
            label = f"{phase_indices[phase]:02d}_{phase}"
            result = await self._wait_for_healthcheck(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                healthcheck=healthcheck,
                label=label,
                artifacts_dir=workspace_artifacts,
            )
            results.append(result)
            if not result.ok:
                _log.info(
                    "validation.healthcheck_failed",
                    workspace_id=workspace_id,
                    healthcheck_name=healthcheck.name,
                    healthcheck_kind=healthcheck.kind,
                    target=healthcheck.target(),
                    returncode=result.returncode,
                    reason_code=result.reason_code,
                )
                return ValidationResult(commands=results)

        for index, (phase, command) in enumerate(commands, start=1):
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

    async def _run_alembic_policy(
        self,
        *,
        workspace_id: str,
        policy: ProfileAlembicValidation,
        worktree_path: Path | None,
        label: str,
        artifacts_dir: Path,
    ) -> ValidationCommandResult:
        started = time.monotonic()
        phase = ALEMBIC_MIGRATION_POLICY_PHASE
        base_stream_id = f"validation.{label}"
        stream_ids: dict[str, str | None] = {
            "stdout": f"{base_stream_id}.stdout",
            "stderr": f"{base_stream_id}.stderr",
        }

        if worktree_path is None:
            metadata = _alembic_policy_missing_worktree_metadata(policy)
            reason_code = "ALEMBIC_WORKTREE_REQUIRED"
            policy_failed = True
        else:
            graph_result = await asyncio.to_thread(
                validate_alembic_migration_chain,
                worktree_path,
                policy,
            )
            metadata = alembic_policy_metadata(graph_result, policy=policy)
            reason_code = graph_result.reason_code
            policy_failed = graph_result.status == AlembicGraphValidationStatus.failed or (
                graph_result.status == AlembicGraphValidationStatus.unsupported
                and policy.fail_on_unconfigured
            )

        rendered = json.dumps(metadata, sort_keys=True, indent=2) + "\n"
        stdout = "" if policy_failed else rendered
        stderr = rendered if policy_failed else ""
        stdout_path = artifacts_dir / f"{label}.stdout"
        stderr_path = artifacts_dir / f"{label}.stderr"
        await asyncio.to_thread(
            _write_alembic_policy_artifacts,
            stdout_path,
            stderr_path,
            stdout,
            stderr,
        )

        if self._log_store is not None:
            sinks = await self._log_store.open_command_streams(
                workspace_id=workspace_id,
                base_stream_id=base_stream_id,
                source="validation",
                name=f"{phase} {label}",
            )
            try:
                if stdout:
                    await sinks.write_stdout(stdout)
                if stderr:
                    await sinks.write_stderr(stderr)
            finally:
                await sinks.close()

        _log.info(
            "validation.alembic_migration_policy_checked",
            workspace_id=workspace_id,
            reason_code=reason_code,
            status=metadata.get("status"),
            policy_failed=policy_failed,
        )
        return ValidationCommandResult(
            command=ALEMBIC_MIGRATION_POLICY_COMMAND,
            returncode=1 if policy_failed else 0,
            duration_seconds=time.monotonic() - started,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            phase=phase,
            reason_code=reason_code,
            stream_ids=stream_ids,
            policy_failed=policy_failed,
            metadata=metadata,
        )

    async def _wait_for_healthcheck(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        healthcheck: ProfileHealthCheck,
        label: str,
        artifacts_dir: Path,
    ) -> ValidationCommandResult:
        started = time.monotonic()
        deadline = started + healthcheck.timeout_seconds
        attempts = 0
        latest: ValidationCommandResult | None = None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and latest is not None:
                break
            attempts += 1
            remaining_before_attempt = remaining
            attempt_timeout = _healthcheck_attempt_timeout(healthcheck, remaining)
            result = await self._exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=_healthcheck_cli_args(healthcheck),
                label=label,
                artifacts_dir=artifacts_dir,
                phase="healthcheck",
                timeout_seconds=attempt_timeout,
                is_retry=(attempts > 1),
                output_prefix=_healthcheck_attempt_prefix(
                    attempt=attempts,
                    elapsed_seconds=time.monotonic() - started,
                ),
            )
            latest = result
            if result.ok:
                final = replace(
                    result,
                    command=healthcheck.display_command(),
                    reason_code=HEALTHCHECK_OK,
                    retry_count=attempts - 1,
                    metadata=_healthcheck_metadata(
                        healthcheck=healthcheck,
                        attempts=attempts,
                        result=result,
                        reason_code=HEALTHCHECK_OK,
                    ),
                )
                _log.info(
                    "validation.healthcheck_ready",
                    workspace_id=workspace_id,
                    healthcheck_name=healthcheck.name,
                    healthcheck_kind=healthcheck.kind,
                    attempts=attempts,
                    timeout_seconds=healthcheck.timeout_seconds,
                )
                return final

            if result.reason_code == "PHASE_TIMEOUT":
                remaining = deadline - time.monotonic()
                if remaining <= 0 or attempt_timeout >= remaining_before_attempt:
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(healthcheck.interval_seconds, remaining))

        if latest is None:  # pragma: no cover - impossible with positive timeout_seconds.
            raise RuntimeError("healthcheck wait loop ended before first attempt")

        reason_code = _healthcheck_failure_reason(healthcheck, latest)
        diagnostic = _healthcheck_failure_diagnostic(
            healthcheck=healthcheck,
            attempts=attempts,
            reason_code=reason_code,
        )
        await self._append_healthcheck_stderr(
            workspace_id=workspace_id,
            result=latest,
            diagnostic=diagnostic,
        )
        return replace(
            latest,
            command=healthcheck.display_command(),
            reason_code=reason_code,
            retry_count=max(0, attempts - 1),
            metadata=_healthcheck_metadata(
                healthcheck=healthcheck,
                attempts=attempts,
                result=latest,
                reason_code=reason_code,
            ),
        )

    async def _append_healthcheck_stderr(
        self,
        *,
        workspace_id: str,
        result: ValidationCommandResult,
        diagnostic: str,
    ) -> None:
        with result.stderr_path.open("a", encoding="utf-8") as handle:
            handle.write(diagnostic)
        if self._log_store is None:
            return
        stderr_stream_id = result.stream_ids.get("stderr")
        if not isinstance(stderr_stream_id, str) or not stderr_stream_id.endswith(".stderr"):
            return
        await self._log_store.append_to_stream(
            workspace_id=workspace_id,
            stream_id=stderr_stream_id,
            source="validation",
            fd="stderr",
            data=diagnostic,
            close_after_append=True,
        )

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
        phase: str = "coverage",
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
            phase_indices[phase] = phase_indices.get(phase, 0) + 1
            label = f"{phase_indices[phase]:02d}_{phase}"
            command_result = await self._exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=["sh", "-lc", _VENV_ACTIVATE_PREAMBLE + coverage.command.command],
                label=label,
                artifacts_dir=artifacts_dir,
                phase=phase,
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
        timeout_seconds: float | None = None,
        is_retry: bool = False,
        output_prefix: str | None = None,
    ) -> ValidationCommandResult:
        invocation = build_tracked_compose_exec(
            compose_project=compose_project,
            compose_file=compose_file,
            cli_args=cli_args,
            source="validation",
            label=label,
        )
        docker_args = invocation.args
        started = time.monotonic()
        reason_code = "COMMAND_FAILED"
        base_stream_id = f"validation.{label}"
        stream_ids: dict[str, str | None] = {
            "stdout": f"{base_stream_id}.stdout",
            "stderr": f"{base_stream_id}.stderr",
        }
        sinks = None
        timed_out = False
        if self._log_store is not None:
            sinks = await self._log_store.open_command_streams(
                workspace_id=artifacts_dir.name,
                base_stream_id=base_stream_id,
                source="validation",
                name=f"{phase} {label}",
            )
        try:
            if output_prefix is not None and sinks is not None:
                await sinks.write_stdout(output_prefix)
                await sinks.write_stderr(output_prefix)
            run_streaming = getattr(self._runner, "run_streaming", None)
            try:
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
                    if run_streaming is not None:
                        result = await run_streaming(
                            docker_args,
                            on_stdout=sinks.write_stdout if sinks is not None else None,
                            on_stderr=sinks.write_stderr if sinks is not None else None,
                            wall_timeout_seconds=timeout_seconds,
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
                timed_out = True
                result = CommandResult(
                    returncode=124,
                    stdout="",
                    stderr=f"command timed out after {timeout_seconds}s",
                )
                reason_code = "PHASE_TIMEOUT"
                if sinks is not None:
                    await sinks.write_stderr(result.stderr)
            except asyncio.CancelledError:
                await cleanup_compose_exec_invocation_after_cancellation(
                    self._runner,
                    invocation,
                    workspace_id=artifacts_dir.name,
                )
                raise
            if timed_out or _compose_exec_timed_out(result):
                reason_code = "PHASE_TIMEOUT"
                await cleanup_compose_exec_invocation(
                    self._runner,
                    invocation,
                    workspace_id=artifacts_dir.name,
                )
        finally:
            if sinks is not None:
                await sinks.close()
        duration = time.monotonic() - started

        stdout_path = artifacts_dir / f"{label}.stdout"
        stderr_path = artifacts_dir / f"{label}.stderr"
        mode = "a" if is_retry else "w"
        with stdout_path.open(mode, encoding="utf-8") as f:
            if output_prefix is not None:
                f.write(output_prefix)
            f.write(result.stdout)
        with stderr_path.open(mode, encoding="utf-8") as f:
            if output_prefix is not None:
                f.write(output_prefix)
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


def _compose_exec_timed_out(result: CommandResult) -> bool:
    return result.reason_code in {COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON}


def _alembic_policy_missing_worktree_metadata(
    policy: ProfileAlembicValidation,
) -> dict[str, object]:
    return {
        "status": "failed",
        "reason_code": "ALEMBIC_WORKTREE_REQUIRED",
        "heads": [],
        "message": "Alembic migration-chain policy requires a workspace worktree path.",
        "details": {},
        "findings": [
            {
                "reason_code": "ALEMBIC_WORKTREE_REQUIRED",
                "message": "Alembic migration-chain policy requires a workspace worktree path.",
                "details": {},
            }
        ],
        "policy": {
            "enabled": policy.enabled,
            "config_path": policy.config_path,
            "script_location": policy.script_location,
            "fail_on_unconfigured": policy.fail_on_unconfigured,
        },
    }


def _write_alembic_policy_artifacts(
    stdout_path: Path,
    stderr_path: Path,
    stdout: str,
    stderr: str,
) -> None:
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")


def _display_command(cli_args: list[str]) -> str:
    if len(cli_args) == 3 and cli_args[0] == "sh":
        shell_cmd = cli_args[2]
        if shell_cmd.startswith(_VENV_ACTIVATE_PREAMBLE):
            shell_cmd = shell_cmd[len(_VENV_ACTIVATE_PREAMBLE) :]
        return shell_cmd
    return " ".join(shlex.quote(a) for a in cli_args)


def _healthcheck_cli_args(healthcheck: ProfileHealthCheck) -> list[str]:
    if healthcheck.command is not None:
        return ["sh", "-lc", _VENV_ACTIVATE_PREAMBLE + healthcheck.command]
    if healthcheck.url is None:
        return [
            "python",
            "-c",
            "import sys; print('invalid healthcheck configuration', file=sys.stderr); sys.exit(2)",
        ]
    return [
        "python",
        "-c",
        _HTTP_HEALTHCHECK_SCRIPT,
        healthcheck.method,
        healthcheck.url,
        str(healthcheck.expected_status),
        str(float(healthcheck.attempt_timeout_seconds or healthcheck.timeout_seconds)),
    ]


def _healthcheck_attempt_timeout(
    healthcheck: ProfileHealthCheck,
    remaining_seconds: float,
) -> float:
    if remaining_seconds <= 0:
        return 0.001
    if healthcheck.attempt_timeout_seconds is None:
        return max(0.001, remaining_seconds)
    return max(0.001, min(healthcheck.attempt_timeout_seconds, remaining_seconds))


def _healthcheck_attempt_prefix(*, attempt: int, elapsed_seconds: float) -> str:
    return f"[healthcheck attempt {attempt} elapsed {_format_seconds(elapsed_seconds)}s]\n"


def _healthcheck_failure_reason(
    healthcheck: ProfileHealthCheck,
    latest: ValidationCommandResult,
) -> str:
    if latest.reason_code == "PHASE_TIMEOUT" or latest.returncode == 124:
        return HEALTHCHECK_TIMEOUT
    if healthcheck.url is not None:
        return HEALTHCHECK_HTTP_STATUS_MISMATCH
    if healthcheck.command is not None:
        return HEALTHCHECK_COMMAND_FAILED
    return HEALTHCHECK_INVALID_CONFIGURATION


def _healthcheck_failure_diagnostic(
    *,
    healthcheck: ProfileHealthCheck,
    attempts: int,
    reason_code: str,
) -> str:
    timeout = _format_seconds(healthcheck.timeout_seconds)
    if reason_code == HEALTHCHECK_TIMEOUT:
        summary = f"health check {healthcheck.name} timed out after {timeout}s"
    else:
        summary = f"health check {healthcheck.name} failed after {attempts} attempt(s)"
    return (
        "\n"
        f"{summary}; target={healthcheck.target()}; "
        f"reason_code={reason_code}; attempts={attempts}\n"
    )


def _healthcheck_metadata(
    *,
    healthcheck: ProfileHealthCheck,
    attempts: int,
    result: ValidationCommandResult,
    reason_code: str,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "healthcheck_name": healthcheck.name,
        "healthcheck_kind": healthcheck.kind or ("http" if healthcheck.url else "command"),
        "target": healthcheck.target(),
        "attempts": attempts,
        "timeout_seconds": healthcheck.timeout_seconds,
        "interval_seconds": healthcheck.interval_seconds,
        "reason_code": reason_code,
        "returncode": result.returncode,
        "stream_ids": dict(result.stream_ids),
    }
    if healthcheck.attempt_timeout_seconds is not None:
        metadata["attempt_timeout_seconds"] = healthcheck.attempt_timeout_seconds
    if healthcheck.url is not None:
        metadata["method"] = healthcheck.method
        metadata["expected_status"] = healthcheck.expected_status
    return metadata


def _format_seconds(value: float) -> str:
    return f"{value:g}"



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


def _missing_line_count(tokens: list[object]) -> int:
    total = 0
    for token in tokens:
        if not isinstance(token, str):
            continue
        token = token.strip()
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start = int(parts[0])
                end = int(parts[1])
                total += max(0, end - start + 1)
            except (ValueError, IndexError):
                total += 1
        else:
            try:
                int(token)
                total += 1
            except ValueError:
                total += 1
    return total


def _parse_term_missing_gaps(paths: list[Path]) -> list[dict[str, object]]:
    gaps: list[dict[str, object]] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                lines = stream.readlines()
        except FileNotFoundError:
            continue

        in_trailer = False
        for line in lines:
            stripped = line.rstrip("\n")
            if _COVERAGE_HEADER_RE.search(stripped):
                in_trailer = True
                continue
            if not in_trailer:
                continue
            match = _COVERAGE_FILE_LINE_RE.match(stripped)
            if match is None:
                continue
            file_name = match.group("file").strip()
            if file_name.lower() == "total":
                continue
            missing_str = match.group("missing").strip()
            missing_lines = [m.strip() for m in missing_str.split(",")] if missing_str else []
            gaps.append({"file": file_name, "missing_lines": missing_lines})

    gaps.sort(key=lambda g: _missing_line_count(g.get("missing_lines", [])), reverse=True)  # type: ignore[arg-type]
    return gaps[:10]


def _coverage_status(*, reason_code: str, enforce: bool) -> str:
    if reason_code == "COVERAGE_OK":
        return "passed"
    if enforce:
        return "failed"
    return "reported"
