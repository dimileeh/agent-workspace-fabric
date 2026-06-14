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
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from awf.common.commands import (
    AsyncCommandRunner,
    CommandResult,
)
from awf.common.compose_exec import (
    ComposeExecCleanupError,
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
    ProfileLintFinding,
    WorkspaceProfile,
)
from awf.runtime.alembic_validation import (
    ALEMBIC_MIGRATION_POLICY_COMMAND,
    ALEMBIC_MIGRATION_POLICY_PHASE,
    alembic_policy_metadata,
    validate_alembic_migration_chain,
)
from awf.runtime.logs import LogStore
from awf.runtime.toolchain_probe import ProbeExecResult, probe_runtime_toolchains
from awf.runtime.validation_coverage import (
    _alembic_policy_missing_worktree_metadata,
    _compose_exec_timed_out,
    _coverage_output_paths,
    _coverage_reason_code,
    _coverage_requested,
    _coverage_status,
    _display_command,
    _final_command_result,
    _healthcheck_attempt_prefix,
    _healthcheck_attempt_timeout,
    _healthcheck_cli_args,
    _healthcheck_failure_diagnostic,
    _healthcheck_failure_reason,
    _healthcheck_metadata,
    _parse_coverage_provider_failure_evidence_from_files,
    _parse_pytest_failure_evidence_from_files,
    _parse_python_coverage_percent_from_files,
    _parse_term_missing_gaps,
    _should_parse_pytest_failure_evidence,
    _write_alembic_policy_artifacts,
    coverage_command_plan,
)
from awf.runtime.validation_setup import (
    _classify_setup_dependency_network_result,
    _setup_dependency_attempt_metadata,
    _setup_dependency_retry_applies,
    _setup_dependency_retry_output_prefix,
    _with_setup_dependency_network_metadata,
    profile_phase_command_plan,
    profile_validation_tool_preflight_findings,
    validate_command_probe_targets,
)
from awf.runtime.validation_types import (
    CoverageCommandPlan,
    ProfileExecutionCommand,
    PytestFailureEvidence,
    SetupDependencyNetworkClassification,
    ValidateToolProbeResult,
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)
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
_COVERAGE_FAIL_UNDER_RE = re.compile(r"(?i)^\s*FAIL\b.*\bcoverage\b.*\bnot reached\b.*$")
_COVERAGE_FAIL_UNDER_PERCENT_RE = re.compile(
    r"(?i)\btotal\s+coverage:\s*(?P<percent>\d+(?:\.\d+)?)%"
)
_COVERAGE_FILE_LINE_RE = re.compile(
    r"^(?P<file>\S.*?)\s+(?P<stmts>\d+)\s+(?P<miss>\d+)\s+(?P<cover>\d+)%\s*(?P<missing>.*?)\s*$"
)
_COVERAGE_HEADER_RE = re.compile(r"(?i)Name\s+Stmts\s+Miss\s+Cover")
_PYTEST_FAILURE_SUMMARY_RE = re.compile(r"^(?P<kind>FAILED|ERROR)\s+(?P<rest>.+)$")
_PYTEST_PROGRESS_PREFIX_RE = re.compile(r"^(?:\[[^\]]+\]\s+)+(?=(?:FAILED|ERROR)\s+)")
_PYTEST_NODE_COMPONENT = r"(?:[^\s:\[]+|\[[^\]]*\])+"
_PYTEST_NODE_ID_RE = re.compile(rf"^(?P<node>[^\s:]+\.py(?:::{_PYTEST_NODE_COMPONENT})*)")
_PYTEST_EVIDENCE_LIMIT = 20
_PYTEST_NODE_ID_LIMIT = 20
_PYTEST_EVIDENCE_MAX_CHARS = 500
HEALTHCHECK_OK = "HEALTHCHECK_OK"
HEALTHCHECK_TIMEOUT = "HEALTHCHECK_TIMEOUT"
HEALTHCHECK_COMMAND_FAILED = "HEALTHCHECK_COMMAND_FAILED"
HEALTHCHECK_HTTP_STATUS_MISMATCH = "HEALTHCHECK_HTTP_STATUS_MISMATCH"
HEALTHCHECK_INVALID_CONFIGURATION = "HEALTHCHECK_INVALID_CONFIGURATION"
PYTEST_TEST_FAILURE = "PYTEST_TEST_FAILURE"
PROFILE_PREFLIGHT_PHASE = "profile_preflight"
PROFILE_VALIDATION_TOOL_UNAVAILABLE = "PROFILE_VALIDATION_TOOL_UNAVAILABLE"
DATABASE_GENERATED_SETUP_FAILED = "DATABASE_GENERATED_SETUP_FAILED"
DATABASE_GENERATED_SETUP_TIMEOUT = "DATABASE_GENERATED_SETUP_TIMEOUT"
DATABASE_REFRESH_FAILED = "DATABASE_REFRESH_FAILED"
DATABASE_REFRESH_TIMEOUT = "DATABASE_REFRESH_TIMEOUT"
SETUP_DEPENDENCY_NETWORK_FAILURE = "SETUP_DEPENDENCY_NETWORK_FAILURE"
SETUP_DEPENDENCY_NETWORK_RETRY = "SETUP_DEPENDENCY_NETWORK_RETRY"
SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED = "SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED"
SETUP_DEPENDENCY_NETWORK_METADATA_KEY = "setup_dependency_network"
DB_GENERATED_SETUP_PHASE = "db_generated_setup"
DB_REFRESH_PHASE = "db_refresh"
# Wall timeout for the non-blocking provision-time toolchain discovery exec. A
# wedged ``docker compose exec`` must never stall the execute()/monitor handoff
# that runs the probe, so the exec is bounded and the tracked process tree torn
# down on timeout (the failure stays silent — see ``probe_runtime_toolchains``).
_TOOLCHAIN_PROBE_TIMEOUT_SECONDS = 30.0
# Wall timeout for the post-timeout cleanup exec. ``cleanup_compose_exec_invocation``
# runs another ``docker compose exec`` via ``runner.run`` with no internal wall
# timeout, so a wedged Docker daemon would let the cleanup itself hang the probe
# (and the handoff) forever — bound it too, and give up cleanup rather than block.
_TOOLCHAIN_PROBE_CLEANUP_TIMEOUT_SECONDS = 30.0
# One ``sh -c`` reachability probe over the ``validate``-tool list. ``command -v``
# checks each tool, and the trailing ``true`` guarantees the exec exits 0 whenever
# the container is reachable — so a non-zero return / timeout unambiguously signals
# a probe-infra failure (``probe_errored``), never a genuine missing tool. Each
# tool reachable -> ``OK <tool>``; genuinely absent -> ``MISSING <tool>``.
_VALIDATE_TOOLCHAIN_PROBE_SCRIPT = (
    'for t in "$@"; do '
    'if command -v "$t" >/dev/null 2>&1; then echo "OK $t"; '
    'else echo "MISSING $t"; fi; '
    "done; true"
)
_VALIDATE_TOOLCHAIN_PROBE_MISSING_RE = re.compile(r"^MISSING (?P<tool>.+)$", re.MULTILINE)
_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT = 1000
_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_SCAN_LIMIT = 4 * _SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT
_SETUP_DEPENDENCY_NETWORK_COMMAND_LIMIT = 500
_SETUP_DEPENDENCY_NETWORK_FAILURE_BLOCK_RADIUS = 6
_SETUP_DEPENDENCY_NETWORK_DEFAULT_RETRY_BUDGET = 2
_SETUP_DEPENDENCY_NETWORK_DEFAULT_BACKOFF_SECONDS = (1.0, 3.0)
_UV_DEV_VALIDATION_TOOLS = frozenset({"mypy", "pre-commit", "pytest", "ruff"})
_UV_OPTION_VALUE_FLAGS = frozenset(
    {
        "--config-setting",
        "--config-settings-package",
        "--default-index",
        "--directory",
        "--env-file",
        "--exclude-newer",
        "--extra",
        "--extra-index-url",
        "--find-links",
        "--from",
        "--group",
        "--index",
        "--index-strategy",
        "--keyring-provider",
        "--link-mode",
        "--no-binary",
        "--no-build",
        "--no-build-package",
        "--no-extra",
        "--only-binary",
        "--only-group",
        "--project",
        "--python",
        "--python-platform",
        "--refresh-package",
        "--resolution",
        "--upgrade-package",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-C",
        "-p",
    }
)
_PROFILE_PHASE_EXECUTION_ORDER = {
    "setup": 0,
    "pre_agent": 1,
    "post_agent": 2,
    "validate": 3,
    "cleanup": 4,
}
_FULL_GATE_SEMAPHORES: dict[int, asyncio.Semaphore] = {}

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


class ValidationRunner:
    """Runs profile phases inside the per-workspace agent container."""

    def __init__(
        self,
        *,
        runner: AsyncCommandRunner,
        artifacts_dir: Path,
        log_store: LogStore | None = None,
        setup_retry_budget: int = _SETUP_DEPENDENCY_NETWORK_DEFAULT_RETRY_BUDGET,
        setup_retry_backoff_seconds: tuple[
            float, ...
        ] = _SETUP_DEPENDENCY_NETWORK_DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._runner = runner
        self._artifacts_dir = artifacts_dir
        self._log_store = log_store
        self._setup_retry_budget = max(0, setup_retry_budget)
        self._setup_retry_backoff_seconds = tuple(
            max(0.0, seconds) for seconds in setup_retry_backoff_seconds
        )

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
        commands = [
            ProfileExecutionCommand(phase="validate", command=ProfileCommand(command=c))
            for c in test_commands
        ]
        return await self._run_commands(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            commands=commands,
            healthchecks=[],
            legacy_command_labels=True,
            coverage=None,
        )

    async def run_profile_tool_preflight(
        self,
        *,
        workspace_id: str,
        profile: WorkspaceProfile,
    ) -> ValidationResult:
        """Fail fast when profile validation commands cannot keep setup tools visible."""
        findings = profile_validation_tool_preflight_findings(profile)
        if not findings:
            return ValidationResult()

        started = time.monotonic()
        workspace_artifacts = self._artifacts_dir / workspace_id
        workspace_artifacts.mkdir(parents=True, exist_ok=True)
        label = "01_profile_preflight"
        base_stream_id = f"validation.{label}"
        stdout_path = workspace_artifacts / f"{label}.stdout"
        stderr_path = workspace_artifacts / f"{label}.stderr"
        metadata: dict[str, object] = {"findings": [finding.as_metadata() for finding in findings]}
        stderr = json.dumps(metadata, sort_keys=True, indent=2) + "\n"
        await asyncio.to_thread(stdout_path.write_text, "", encoding="utf-8")
        await asyncio.to_thread(stderr_path.write_text, stderr, encoding="utf-8")

        stream_ids: dict[str, str | None] = {
            "stdout": f"{base_stream_id}.stdout",
            "stderr": f"{base_stream_id}.stderr",
        }
        if self._log_store is not None:
            sinks = await self._log_store.open_command_streams(
                workspace_id=workspace_id,
                base_stream_id=base_stream_id,
                source="validation",
                name=f"{PROFILE_PREFLIGHT_PHASE} {label}",
            )
            try:
                await sinks.write_stderr(stderr)
            finally:
                await sinks.close()

        _log.info(
            "validation.profile_tool_preflight_failed",
            workspace_id=workspace_id,
            reason_code=PROFILE_VALIDATION_TOOL_UNAVAILABLE,
            finding_count=len(findings),
        )
        result = ValidationCommandResult(
            command="profile validation tool preflight",
            returncode=1,
            duration_seconds=time.monotonic() - started,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            phase=PROFILE_PREFLIGHT_PHASE,
            reason_code=PROFILE_VALIDATION_TOOL_UNAVAILABLE,
            stream_ids=stream_ids,
            policy_failed=True,
            metadata=metadata,
        )
        return ValidationResult(commands=[result])

    async def probe_runtime_toolchain_findings(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
    ) -> tuple[ProfileLintFinding, ...]:
        """Discover declared toolchain versions in the container; return findings.

        Provision-time, non-blocking probe: when the profile declares
        ``runtime.toolchains``, exec the per-language discovery commands inside
        the agent container (reusing the tracked compose-exec path) and run the
        pure ``runtime_toolchain_findings`` helper over the discovered versions.
        Skips entirely (zero exec) when no toolchains are declared.
        """
        if not profile.runtime.toolchains:
            return ()

        async def _exec(cli_args: list[str]) -> ProbeExecResult:
            invocation = build_tracked_compose_exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=cli_args,
                source="toolchain_probe",
                label="toolchain_probe",
            )
            try:
                result = await asyncio.wait_for(
                    self._runner.run(invocation.args),
                    timeout=_TOOLCHAIN_PROBE_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                # The default runner waits on ``proc.communicate()`` with no wall
                # timeout, so a wedged ``docker compose exec`` would otherwise stall
                # this non-blocking probe (and the handoff that runs it) forever.
                # Tear the tracked in-container process tree down, then report a
                # probe-infra failure (non-zero) so the helper stays silent for this
                # language instead of warning falsely.
                # Cleanup already logs any failure; the probe must stay strictly
                # non-blocking, so swallow a cleanup error rather than propagate.
                # The cleanup is itself an unbounded ``docker compose exec``, so a
                # wedged daemon could hang it forever — bound it with its own short
                # wall timeout and abandon cleanup rather than stall the handoff.
                with suppress(ComposeExecCleanupError, TimeoutError):
                    await asyncio.wait_for(
                        cleanup_compose_exec_invocation(
                            self._runner,
                            invocation,
                            workspace_id=workspace_id,
                        ),
                        timeout=_TOOLCHAIN_PROBE_CLEANUP_TIMEOUT_SECONDS,
                    )
                return ProbeExecResult(
                    returncode=124,
                    stdout="",
                    stderr=f"toolchain probe timed out after {_TOOLCHAIN_PROBE_TIMEOUT_SECONDS}s",
                )
            except asyncio.CancelledError:
                await cleanup_compose_exec_invocation_after_cancellation(
                    self._runner,
                    invocation,
                    workspace_id=workspace_id,
                )
                raise
            return ProbeExecResult(
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        return await probe_runtime_toolchains(profile=profile, exec_in_container=_exec)

    async def probe_validate_command_tools(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
    ) -> ValidateToolProbeResult:
        """Check each ``validate`` command's executable resolves on PATH.

        Adopt-pr handoff skips the coding agent, so the profile ``setup`` phase is
        the only provisioning step; this probe runs after setup and reports any
        ``validate`` tool that is still missing so the adoption can fail early and
        clearly instead of dying ``127`` later at ``sync_base_push``. Skips
        entirely (zero exec) when the profile has no probeable validate command.

        Mirrors ``probe_runtime_toolchain_findings``' reachability contract: one
        always-exit-0 in-container exec via the tracked compose-exec path, so a
        non-zero return / timeout / ``OSError`` is classified as a probe-infra
        error (``probe_errored``) and never mis-reported as a profile gap.
        """
        targets = validate_command_probe_targets(profile)
        if not targets:
            return ValidateToolProbeResult()

        invocation = build_tracked_compose_exec(
            compose_project=compose_project,
            compose_file=compose_file,
            # Mirror the shell that real validate commands run under (``sh -lc`` +
            # the ``.venv`` activation preamble; see lines that exec ``command.command``
            # below). Without this, a tool installed only into ``/workspace/.venv/bin``
            # during setup resolves when validate actually runs but is reported MISSING
            # by a bare ``sh -c`` probe, falsely failing adoption with
            # ``PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED``.
            cli_args=[
                "sh",
                "-lc",
                _VENV_ACTIVATE_PREAMBLE + _VALIDATE_TOOLCHAIN_PROBE_SCRIPT,
                "validate_toolchain_probe",
                *[target.tool for target in targets],
            ],
            source="validate_toolchain_probe",
            label="validate_toolchain_probe",
        )
        try:
            result = await asyncio.wait_for(
                self._runner.run(invocation.args),
                timeout=_TOOLCHAIN_PROBE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # A wedged ``docker compose exec`` must not stall this handoff probe.
            # Tear the tracked process tree down (itself bounded, since the
            # cleanup is another unbounded exec) and report a probe-infra error so
            # the caller proceeds instead of falsely failing the adoption.
            with suppress(ComposeExecCleanupError, TimeoutError):
                await asyncio.wait_for(
                    cleanup_compose_exec_invocation(
                        self._runner,
                        invocation,
                        workspace_id=workspace_id,
                    ),
                    timeout=_TOOLCHAIN_PROBE_CLEANUP_TIMEOUT_SECONDS,
                )
            return ValidateToolProbeResult(probe_errored=True)
        except asyncio.CancelledError:
            await cleanup_compose_exec_invocation_after_cancellation(
                self._runner,
                invocation,
                workspace_id=workspace_id,
            )
            raise
        except OSError:
            # The container exec could not spawn at all: a probe-infra failure,
            # not a genuine missing tool.
            return ValidateToolProbeResult(probe_errored=True)

        if result.returncode != 0:
            return ValidateToolProbeResult(probe_errored=True)

        missing_tools = {
            match.group("tool").strip()
            for match in _VALIDATE_TOOLCHAIN_PROBE_MISSING_RE.finditer(result.stdout)
        }
        missing_targets = tuple(target for target in targets if target.tool in missing_tools)
        return ValidateToolProbeResult(missing=missing_targets)

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
        include_coverage: bool = True,
    ) -> ValidationResult:
        """Run the selected profile phases in order."""
        requested_phases = set(phase_names)
        healthchecks = profile.validation.healthchecks if run_healthchecks else []
        coverage = (
            profile.validation.coverage
            if include_coverage and "validate" in requested_phases
            else None
        )
        alembic_policy = profile.validation.alembic if "validate" in requested_phases else None
        healthcheck_before_phase = (
            "validate"
            if profile.database.pre_validation_refresh and "validate" in requested_phases
            else None
        )
        return await self._run_commands(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            commands=profile_phase_command_plan(profile, phase_names),
            healthchecks=list(healthchecks),
            legacy_command_labels=False,
            retry_budget=profile.validation.retry_budget,
            coverage=coverage,
            healthcheck_before_phase=healthcheck_before_phase,
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
        parallel_worker_cpu_limit: int | None = None,
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
            full_gate_concurrency=profile.validation.strategy.full_gate_concurrency,
            parallel_worker_cpu_limit=parallel_worker_cpu_limit,
        )

    async def _run_commands(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        commands: list[ProfileExecutionCommand],
        healthchecks: list[ProfileHealthCheck],
        legacy_command_labels: bool,
        retry_budget: int = 0,
        coverage: ProfileCoverage | None,
        healthcheck_before_phase: str | None = None,
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

        pending_healthchecks = list(healthchecks)
        if healthcheck_before_phase is None:
            failure = await self._run_healthchecks(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                artifacts_dir=workspace_artifacts,
                healthchecks=pending_healthchecks,
                results=results,
                phase_indices=phase_indices,
            )
            pending_healthchecks = []
            if failure is not None:
                return ValidationResult(commands=results)

        for index, step in enumerate(commands, start=1):
            phase = step.phase
            command = step.command
            if (
                healthcheck_before_phase is not None
                and pending_healthchecks
                and phase == healthcheck_before_phase
            ):
                failure = await self._run_healthchecks(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    artifacts_dir=workspace_artifacts,
                    healthchecks=pending_healthchecks,
                    results=results,
                    phase_indices=phase_indices,
                )
                pending_healthchecks = []
                if failure is not None:
                    return ValidationResult(commands=results)

            if legacy_command_labels:
                label = f"cmd_{index:02d}"
            else:
                phase_indices[phase] = phase_indices.get(phase, 0) + 1
                label = f"{phase_indices[phase]:02d}_{phase}"

            flaky_retry_count = 0
            setup_retry_count = 0
            setup_dependency_attempts: list[dict[str, object]] = []
            last_setup_dependency_classification: SetupDependencyNetworkClassification | None = None
            setup_dependency_started = time.monotonic()
            setup_dependency_output_prefix: str | None = None
            while True:
                total_retry_count = setup_retry_count + flaky_retry_count
                result = await self._exec(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    cli_args=["sh", "-lc", _VENV_ACTIVATE_PREAMBLE + command.command],
                    label=label,
                    artifacts_dir=workspace_artifacts,
                    phase=phase,
                    timeout_seconds=command.timeout_seconds,
                    is_retry=(total_retry_count > 0),
                    output_prefix=setup_dependency_output_prefix,
                )
                setup_dependency_output_prefix = None

                if result.ok or not command.required:
                    total_retry_count = setup_retry_count + flaky_retry_count
                    final = _final_command_result(result, step=step, attempts=total_retry_count)
                    if (
                        setup_dependency_attempts
                        and last_setup_dependency_classification is not None
                    ):
                        final = _with_setup_dependency_network_metadata(
                            final,
                            step=step,
                            classification=last_setup_dependency_classification,
                            setup_retry_count=setup_retry_count,
                            flaky_retry_count=flaky_retry_count,
                            total_retry_count=total_retry_count,
                            retry_budget=self._setup_retry_budget,
                            retry_exhausted=False,
                            recovered=result.ok,
                            attempts=setup_dependency_attempts,
                        )
                    results.append(final)
                    break

                setup_dependency_classification = (
                    _classify_setup_dependency_network_result(result)
                    if _setup_dependency_retry_applies(step)
                    else None
                )
                if setup_dependency_classification is not None:
                    failed_attempt = setup_retry_count + 1
                    can_retry = setup_retry_count < self._setup_retry_budget
                    setup_dependency_attempts.append(
                        _setup_dependency_attempt_metadata(
                            classification=setup_dependency_classification,
                            attempt=failed_attempt,
                            retry_number=failed_attempt if can_retry else None,
                        )
                    )
                    last_setup_dependency_classification = setup_dependency_classification
                    if can_retry:
                        setup_retry_count += 1
                        _log.info(
                            "validation.setup_dependency_network_retry",
                            workspace_id=workspace_id,
                            phase=phase,
                            command=setup_dependency_classification.command,
                            package=setup_dependency_classification.package,
                            host=setup_dependency_classification.host,
                            transient_category=(setup_dependency_classification.transient_category),
                            attempt=failed_attempt,
                            retry=setup_retry_count,
                            budget=self._setup_retry_budget,
                            reason_code=SETUP_DEPENDENCY_NETWORK_RETRY,
                        )
                        delay = self._setup_dependency_retry_delay(setup_retry_count)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        setup_dependency_output_prefix = _setup_dependency_retry_output_prefix(
                            retry_number=setup_retry_count,
                            elapsed_seconds=time.monotonic() - setup_dependency_started,
                        )
                        continue

                    total_retry_count = setup_retry_count + flaky_retry_count
                    result = _with_setup_dependency_network_metadata(
                        result,
                        step=step,
                        classification=setup_dependency_classification,
                        setup_retry_count=setup_retry_count,
                        flaky_retry_count=flaky_retry_count,
                        total_retry_count=total_retry_count,
                        retry_budget=self._setup_retry_budget,
                        retry_exhausted=True,
                        recovered=False,
                        attempts=setup_dependency_attempts,
                    )
                    results.append(result)
                    _log.info(
                        "validation.setup_dependency_network_retry_exhausted",
                        workspace_id=workspace_id,
                        phase=phase,
                        command=setup_dependency_classification.command,
                        package=setup_dependency_classification.package,
                        host=setup_dependency_classification.host,
                        transient_category=setup_dependency_classification.transient_category,
                        retry_count=setup_retry_count,
                        budget=self._setup_retry_budget,
                        reason_code=SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
                    )
                    return ValidationResult(commands=results)

                # 124: command timeout. > 128: killed by signal (e.g., 137 OOM kill).
                # We treat most signal exits as potentially flaky infrastructure events,
                # accepting the trade-off that deterministic failures like SIGILL or SIGABRT
                # might be needlessly retried.
                is_flaky = result.returncode == 124 or result.returncode > 128
                if is_flaky and flaky_retry_count < retry_budget:
                    flaky_retry_count += 1
                    _log.info(
                        "validation.phase_command_flaky_retry",
                        workspace_id=workspace_id,
                        phase=phase,
                        command=command.command,
                        returncode=result.returncode,
                        attempt=flaky_retry_count,
                        budget=retry_budget,
                    )
                    continue

                total_retry_count = setup_retry_count + flaky_retry_count
                if (
                    is_flaky
                    and flaky_retry_count >= retry_budget
                    and retry_budget > 0
                    and not step.database_hook
                ):
                    result = ValidationCommandResult(
                        command=result.command,
                        returncode=result.returncode,
                        duration_seconds=result.duration_seconds,
                        stdout_path=result.stdout_path,
                        stderr_path=result.stderr_path,
                        phase=result.phase,
                        reason_code="VALIDATION_RETRY_EXHAUSTED",
                        stream_ids=result.stream_ids,
                        retry_count=total_retry_count,
                    )
                else:
                    result = _final_command_result(
                        result,
                        step=step,
                        attempts=total_retry_count,
                    )
                if setup_dependency_attempts and last_setup_dependency_classification is not None:
                    result = _with_setup_dependency_network_metadata(
                        result,
                        step=step,
                        classification=last_setup_dependency_classification,
                        setup_retry_count=setup_retry_count,
                        flaky_retry_count=flaky_retry_count,
                        total_retry_count=total_retry_count,
                        retry_budget=self._setup_retry_budget,
                        retry_exhausted=False,
                        recovered=False,
                        attempts=setup_dependency_attempts,
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

        if healthcheck_before_phase is not None and pending_healthchecks:
            failure = await self._run_healthchecks(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                artifacts_dir=workspace_artifacts,
                healthchecks=pending_healthchecks,
                results=results,
                phase_indices=phase_indices,
            )
            if failure is not None:
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
                full_gate_concurrency=0,
            )

        return ValidationResult(commands=results, coverage=coverage_result)

    def _setup_dependency_retry_delay(self, retry_number: int) -> float:
        if not self._setup_retry_backoff_seconds:
            return 0.0
        index = min(max(retry_number - 1, 0), len(self._setup_retry_backoff_seconds) - 1)
        return self._setup_retry_backoff_seconds[index]

    async def _run_healthchecks(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        artifacts_dir: Path,
        healthchecks: list[ProfileHealthCheck],
        results: list[ValidationCommandResult],
        phase_indices: dict[str, int],
    ) -> ValidationCommandResult | None:
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
                artifacts_dir=artifacts_dir,
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
                return result
        return None

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

        rendered = json.dumps(metadata, sort_keys=True, indent=2, default=str) + "\n"
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
        full_gate_concurrency: int = 0,
        parallel_worker_cpu_limit: int | None = None,
    ) -> ValidationCoverageResult:
        if full_gate_concurrency > 0:
            semaphore = _FULL_GATE_SEMAPHORES.setdefault(
                full_gate_concurrency, asyncio.Semaphore(full_gate_concurrency)
            )
            async with semaphore:
                return await self._collect_coverage_unthrottled(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    coverage=coverage,
                    artifacts_dir=artifacts_dir,
                    results=results,
                    phase_indices=phase_indices,
                    phase=phase,
                    parallel_worker_cpu_limit=parallel_worker_cpu_limit,
                )
        return await self._collect_coverage_unthrottled(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            coverage=coverage,
            artifacts_dir=artifacts_dir,
            results=results,
            phase_indices=phase_indices,
            phase=phase,
            parallel_worker_cpu_limit=parallel_worker_cpu_limit,
        )

    async def _collect_coverage_unthrottled(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        coverage: ProfileCoverage,
        artifacts_dir: Path,
        results: list[ValidationCommandResult],
        phase_indices: dict[str, int],
        phase: str,
        parallel_worker_cpu_limit: int | None = None,
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
        command_plan = (
            CoverageCommandPlan(command=coverage.command.command) if coverage.command else None
        )
        if coverage.command is not None:
            command_plan = coverage_command_plan(
                coverage,
                parallel_worker_cpu_limit=parallel_worker_cpu_limit,
            )
            phase_indices[phase] = phase_indices.get(phase, 0) + 1
            label = f"{phase_indices[phase]:02d}_{phase}"
            command_result = await self._exec(
                compose_project=compose_project,
                compose_file=compose_file,
                cli_args=["sh", "-lc", _VENV_ACTIVATE_PREAMBLE + command_plan.command],
                label=label,
                artifacts_dir=artifacts_dir,
                phase=phase,
                timeout_seconds=coverage.command.timeout_seconds,
            )
            coverage_outputs = [command_result]
        else:
            coverage_outputs = results

        output_paths = _coverage_output_paths(coverage_outputs)
        percent = _parse_python_coverage_percent_from_files(output_paths)
        gaps = _parse_term_missing_gaps(output_paths)
        provider_failure_evidence = _parse_coverage_provider_failure_evidence_from_files(
            output_paths
        )
        pytest_evidence = (
            _parse_pytest_failure_evidence_from_files(output_paths)
            if _should_parse_pytest_failure_evidence(command_result)
            else PytestFailureEvidence()
        )
        reason_code = _coverage_reason_code(
            percent=percent,
            minimum_percent=coverage.minimum_percent,
            command_result=command_result,
            has_pytest_failures=pytest_evidence.present,
            has_provider_fail_under=bool(provider_failure_evidence),
        )
        status = _coverage_status(reason_code=reason_code, enforce=coverage.enforce)
        policy_failed = status == "failed"
        if command_result is not None:
            command_reason_code = PYTEST_TEST_FAILURE if pytest_evidence.present else reason_code
            command_metadata = dict(command_result.metadata)
            if pytest_evidence.node_ids:
                command_metadata["failing_test_node_ids"] = pytest_evidence.node_ids
            if pytest_evidence.evidence:
                command_metadata["failing_test_evidence"] = pytest_evidence.evidence
            if pytest_evidence.present:
                command_metadata["coverage_reason_code"] = reason_code
            if provider_failure_evidence:
                command_metadata["provider_failure_evidence"] = provider_failure_evidence
            if command_plan is not None and command_plan.parallel_workers_requested is not None:
                command_metadata["parallel_workers_requested"] = (
                    command_plan.parallel_workers_requested
                )
                command_metadata["parallel_workers_effective"] = (
                    command_plan.parallel_workers_effective
                )
                command_metadata["parallel_distribution"] = command_plan.parallel_distribution
            command_result = replace(
                command_result,
                reason_code=command_reason_code,
                policy_failed=policy_failed,
                metadata=command_metadata,
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
            gap_count=len(gaps),
        )
        return ValidationCoverageResult(
            provider=coverage.provider,
            percent=percent,
            minimum_percent=coverage.minimum_percent,
            enforce=coverage.enforce,
            status=status,
            reason_code=reason_code,
            command_result=command_result,
            gaps=gaps,
            failing_test_node_ids=pytest_evidence.node_ids,
            failing_test_evidence=pytest_evidence.evidence,
            provider_failure_evidence=provider_failure_evidence,
            parallel_workers_requested=(
                command_plan.parallel_workers_requested if command_plan is not None else None
            ),
            parallel_workers_effective=(
                command_plan.parallel_workers_effective if command_plan is not None else None
            ),
            parallel_distribution=(
                command_plan.parallel_distribution if command_plan is not None else None
            ),
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
            captured_stdout=result.stdout,
            captured_stderr=result.stderr,
        )
