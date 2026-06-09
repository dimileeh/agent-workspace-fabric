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
from dataclasses import replace
from pathlib import Path

from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    CommandResult,
)
from awf.profiles.models import (
    ProfileAlembicValidation,
    ProfileCoverage,
    ProfileHealthCheck,
)
from awf.runtime.validation_types import (
    CoverageCommandPlan,
    ProfileExecutionCommand,
    PytestFailureEvidence,
    ValidationCommandResult,
)

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
DATABASE_GENERATED_SETUP_FAILED = "DATABASE_GENERATED_SETUP_FAILED"
DATABASE_GENERATED_SETUP_TIMEOUT = "DATABASE_GENERATED_SETUP_TIMEOUT"
DATABASE_REFRESH_FAILED = "DATABASE_REFRESH_FAILED"
DATABASE_REFRESH_TIMEOUT = "DATABASE_REFRESH_TIMEOUT"
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


def _compose_exec_timed_out(result: CommandResult) -> bool:
    return result.reason_code in {COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON}


def _read_text_if_present(path: Path) -> str | None:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
        return None
    except OSError:
        return None


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


def _final_command_result(
    result: ValidationCommandResult,
    *,
    step: ProfileExecutionCommand,
    attempts: int,
) -> ValidationCommandResult:
    reason_code = result.reason_code
    if step.database_hook and not result.ok:
        reason_code = _database_hook_failure_reason(step, result.reason_code)
    return replace(
        result,
        reason_code=reason_code,
        retry_count=attempts,
        required=step.command.required,
        metadata=_execution_command_metadata(step),
        captured_stdout=None,
        captured_stderr=None,
    )


def _database_hook_failure_reason(
    step: ProfileExecutionCommand,
    reason_code: str,
) -> str:
    if step.hook_kind == "generated_setup":
        if reason_code == "PHASE_TIMEOUT":
            return DATABASE_GENERATED_SETUP_TIMEOUT
        return DATABASE_GENERATED_SETUP_FAILED
    if reason_code == "PHASE_TIMEOUT":
        return DATABASE_REFRESH_TIMEOUT
    return DATABASE_REFRESH_FAILED


def _execution_command_metadata(step: ProfileExecutionCommand) -> dict[str, object]:
    if not step.database_hook:
        return {}
    return {
        "database_hook": True,
        "hook_kind": step.hook_kind,
        "timeout_seconds": step.command.timeout_seconds,
    }


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


def coverage_command_plan(
    coverage: ProfileCoverage,
    *,
    parallel_worker_cpu_limit: int | None = None,
) -> CoverageCommandPlan:
    if coverage.command is None:
        return CoverageCommandPlan(command="")
    command = coverage.command.command
    requested = coverage.parallel_workers
    if requested is None:
        return CoverageCommandPlan(command=command)

    effective = _effective_parallel_workers(
        requested=requested,
        profile_max=coverage.parallel_worker_max,
        cpu_limit=parallel_worker_cpu_limit,
    )
    distribution = "loadscope"
    injected = _inject_pytest_parallel_workers(
        command,
        workers=effective,
        distribution=distribution,
    )
    return CoverageCommandPlan(
        command=injected,
        parallel_workers_requested=requested,
        parallel_workers_effective=effective if injected != command else None,
        parallel_distribution=distribution if injected != command else None,
    )


def _effective_parallel_workers(
    *,
    requested: int,
    profile_max: int | None,
    cpu_limit: int | None,
) -> int:
    limits = [requested]
    if profile_max is not None:
        limits.append(profile_max)
    if cpu_limit is not None:
        limits.append(max(1, cpu_limit))
    return max(1, min(limits))


def _inject_pytest_parallel_workers(
    command: str,
    *,
    workers: int,
    distribution: str,
) -> str:
    if not _is_pytest_coverage_command(command):
        return command
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    pytest_index = _pytest_token_index(tokens)
    if pytest_index is None:
        return command
    injected = [
        *tokens[: pytest_index + 1],
        "-n",
        str(workers),
        f"--dist={distribution}",
        *tokens[pytest_index + 1 :],
    ]
    return shlex.join(injected)


def _pytest_token_index(tokens: list[str]) -> int | None:
    for index, token in enumerate(tokens):
        if _command_token_name(token) in {"pytest", "py.test"}:
            return index
    return None


def _coverage_requested(coverage: ProfileCoverage) -> bool:
    return coverage.command is not None


def _coverage_output_paths(results: list[ValidationCommandResult]) -> list[Path]:
    paths: list[Path] = []
    for result in results:
        paths.extend((result.stdout_path, result.stderr_path))
    return paths


def _should_parse_pytest_failure_evidence(
    command_result: ValidationCommandResult | None,
) -> bool:
    return (
        command_result is not None
        and command_result.returncode != 0
        and _is_pytest_coverage_command(command_result.command)
    )


def _is_pytest_coverage_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    token_names = [_command_token_name(token) for token in tokens]
    invokes_pytest = any(token in {"pytest", "py.test"} for token in token_names)
    if not invokes_pytest:
        return False

    if any(_is_pytest_cov_option(token) for token in tokens):
        return True

    return _runs_pytest_under_coverage(token_names)


def _command_token_name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _is_pytest_cov_option(token: str) -> bool:
    return token == "--cov" or token.startswith(("--cov=", "--cov-"))


def _runs_pytest_under_coverage(token_names: list[str]) -> bool:
    for index, token in enumerate(token_names):
        if token != "coverage":
            continue
        remaining = token_names[index + 1 :]
        if "run" in remaining and any(item in {"pytest", "py.test"} for item in remaining):
            return True
    return False


def _parse_python_coverage_percent_from_files(paths: list[Path]) -> float | None:
    total_percent: float | None = None
    fail_under_percent: float | None = None
    summary_percent: float | None = None
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            pending_fail_under_lines = 0
            recent_total_coverage_percent: float | None = None
            recent_total_coverage_lines = 0
            for line in stream:
                fail_under_line = _COVERAGE_FAIL_UNDER_RE.match(line) is not None
                fail_under_match = _COVERAGE_FAIL_UNDER_PERCENT_RE.search(line)
                if fail_under_line and fail_under_match:
                    fail_under_percent = float(fail_under_match.group("percent"))
                    continue
                if fail_under_line:
                    if recent_total_coverage_percent is not None:
                        fail_under_percent = recent_total_coverage_percent
                    else:
                        pending_fail_under_lines = 2
                    continue
                if pending_fail_under_lines > 0:
                    if fail_under_match:
                        fail_under_percent = float(fail_under_match.group("percent"))
                        pending_fail_under_lines = 0
                        continue
                    pending_fail_under_lines -= 1
                if fail_under_match:
                    recent_total_coverage_percent = float(fail_under_match.group("percent"))
                    recent_total_coverage_lines = 2
                total_match = _COVERAGE_TOTAL_RE.search(line)
                if total_match:
                    total_percent = float(total_match.group("percent"))
                    continue
                summary_match = _COVERAGE_SUMMARY_RE.search(line)
                if summary_match:
                    summary_percent = float(summary_match.group("percent"))
                if not fail_under_match and recent_total_coverage_lines > 0:
                    recent_total_coverage_lines -= 1
                    if recent_total_coverage_lines == 0:
                        recent_total_coverage_percent = None

    if fail_under_percent is not None:
        return fail_under_percent
    return total_percent if total_percent is not None else summary_percent


def _parse_coverage_provider_failure_evidence_from_files(paths: list[Path]) -> list[str]:
    evidence: list[str] = []
    for path in paths:
        try:
            stream = path.open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        with stream:
            for raw_line in stream:
                line = raw_line.strip()
                if not line:
                    continue
                if _COVERAGE_FAIL_UNDER_RE.match(line):
                    _append_unique_capped(
                        evidence,
                        _truncate_pytest_evidence_line(line),
                        limit=_PYTEST_EVIDENCE_LIMIT,
                    )
    return evidence


def _parse_pytest_failure_evidence_from_files(paths: list[Path]) -> PytestFailureEvidence:
    node_ids: list[str] = []
    evidence: list[str] = []
    fallback_evidence: list[str] = []
    for path in paths:
        try:
            stream = path.open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        with stream:
            for raw_line in stream:
                line = raw_line.rstrip()
                if not line:
                    continue
                summary_line = line.lstrip()
                normalized_summary_line = _normalize_pytest_summary_line(summary_line)
                summary_match = _PYTEST_FAILURE_SUMMARY_RE.match(normalized_summary_line)
                if summary_match is not None:
                    _append_unique_capped(
                        evidence,
                        _truncate_pytest_evidence_line(summary_line),
                        limit=_PYTEST_EVIDENCE_LIMIT,
                    )
                    rest = summary_match.group("rest")
                    node_id = _pytest_node_id_from_text(
                        rest,
                        allow_file_level=summary_match.group("kind") == "ERROR",
                    )
                    if node_id is not None:
                        _append_unique_capped(
                            node_ids,
                            node_id,
                            limit=_PYTEST_NODE_ID_LIMIT,
                        )
                    continue
                if _looks_like_pytest_fallback_evidence(line):
                    _append_unique_capped(
                        fallback_evidence,
                        _truncate_pytest_evidence_line(line),
                        limit=_PYTEST_EVIDENCE_LIMIT,
                    )

    for line in fallback_evidence:
        _append_unique_capped(evidence, line, limit=_PYTEST_EVIDENCE_LIMIT)
    return PytestFailureEvidence(node_ids=node_ids, evidence=evidence)


def _normalize_pytest_summary_line(line: str) -> str:
    return _PYTEST_PROGRESS_PREFIX_RE.sub("", line)


def _pytest_node_id_from_text(value: str, *, allow_file_level: bool = False) -> str | None:
    text = value.lstrip()
    match = _PYTEST_NODE_ID_RE.match(text)
    if match is None:
        return None
    node_id = _strip_pytest_node_id_suffix(match.group("node"))
    if "::" not in node_id:
        if text[match.end() :].startswith("::"):
            return None
        if not allow_file_level:
            return None
    return node_id


def _strip_pytest_node_id_suffix(node_id: str) -> str:
    stripped = node_id.rstrip(".,;")
    # A matched "::" segment always requires a component, so only a single
    # trailing ":" can be a pytest/xdist message separator.
    if stripped.endswith(":") and not stripped.endswith("::"):
        stripped = stripped[:-1]
    return stripped


def _looks_like_pytest_fallback_evidence(line: str) -> bool:
    return line.startswith(("E   ", "E\t"))


def _truncate_pytest_evidence_line(line: str) -> str:
    if len(line) <= _PYTEST_EVIDENCE_MAX_CHARS:
        return line
    return line[: _PYTEST_EVIDENCE_MAX_CHARS - 3] + "..."


def _append_unique_capped(items: list[str], value: str, *, limit: int) -> None:
    if len(items) >= limit or value in items:
        return
    items.append(value)


def _coverage_reason_code(
    *,
    percent: float | None,
    minimum_percent: float,
    command_result: ValidationCommandResult | None,
    has_pytest_failures: bool = False,
    has_provider_fail_under: bool = False,
) -> str:
    if percent is None:
        if has_pytest_failures:
            return "COVERAGE_NOT_FOUND"
        if command_result is not None and command_result.returncode != 0:
            return "COVERAGE_COMMAND_FAILED"
        return "COVERAGE_NOT_FOUND"
    if percent < minimum_percent:
        return "COVERAGE_BELOW_THRESHOLD"
    if has_provider_fail_under:
        return "COVERAGE_FAIL_UNDER_NOT_REACHED"
    if command_result is not None and command_result.returncode != 0 and not has_pytest_failures:
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
