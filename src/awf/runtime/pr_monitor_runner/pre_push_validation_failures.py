"""Failure classification helpers for PR monitor pre-push validation.

These pure functions operate on a :class:`ValidationResult` to select the
command failure that should drive diagnostics, repair prompts, and persisted
reason codes. They are split out from ``pre_push_validation`` to keep that
orchestration module under the first-party file-size guardrail; they carry no
``self`` dependency and are unit-testable in isolation.
"""

from __future__ import annotations

from awf.control.executor.helpers import _validation_run_reason_code
from awf.runtime.validation_types import (
    ValidationCommandResult,
    ValidationResult,
)


def _failed_pre_push_commands(result: ValidationResult) -> tuple[ValidationCommandResult, ...]:
    """Return failed command-like records from a validation result."""
    failures: list[ValidationCommandResult] = []
    if result.migration is not None and not result.migration.ok:
        failures.append(result.migration)
    failures.extend(command for command in result.commands if command.blocks_validation)
    coverage_command = result.coverage.command_result if result.coverage is not None else None
    if coverage_command is not None and not coverage_command.ok:
        failures.append(coverage_command)
    return tuple(failures)


def _first_real_pre_push_failure(result: ValidationResult) -> ValidationCommandResult | None:
    """Return the first non-127 failure, giving real lint/test failures precedence."""
    failures = _failed_pre_push_commands(result)
    return _first_real_pre_push_failure_for_result(result, failures)


def _first_failure_outside_collected_failures(
    result: ValidationResult,
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return ``first_failure`` when it is not represented in collected commands."""
    first_failure = result.first_failure
    if first_failure is None:
        return None
    if any(first_failure is failure for failure in failures):
        return None
    return first_failure


def _first_real_pre_push_failure_for_result(
    result: ValidationResult,
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return the first non-127 failure across command and provider records.

    Provider-level ``first_failure`` may describe a policy failure whose
    underlying command succeeded, such as coverage below threshold with
    ``ok=True`` and ``returncode=0``.
    """
    real_failure = _first_real_pre_push_failure_from_failures(failures)
    if real_failure is not None:
        return real_failure
    first_failure = _first_failure_outside_collected_failures(result, failures)
    if first_failure is not None and first_failure.returncode != 127:
        return first_failure
    return None


def _first_real_pre_push_failure_from_failures(
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return the first non-127 failure from an already collected failure tuple."""
    return next(
        (failure for failure in failures if failure.returncode != 127),
        None,
    )


def _pure_toolchain_missing_failure(
    result: ValidationResult,
) -> ValidationCommandResult | None:
    """Return the first 127 failure only when all command failures are command-not-found.

    Mixed results are treated as genuine validation failures so a real lint/test
    failure is not hidden behind an earlier missing-tool diagnostic.
    """
    failures = _failed_pre_push_commands(result)
    return _pure_toolchain_missing_failure_for_result(result, failures)


def _pure_toolchain_missing_failure_for_result(
    result: ValidationResult,
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return a pure 127 failure, including command-less provider failures."""
    first_failure = _first_failure_outside_collected_failures(result, failures)
    if first_failure is not None and first_failure.returncode != 127:
        return None
    toolchain_failure = _pure_toolchain_missing_failure_from_failures(failures)
    if toolchain_failure is not None:
        return toolchain_failure
    if failures:
        return None
    if first_failure is not None and first_failure.returncode == 127:
        return first_failure
    return None


def _pure_toolchain_missing_failure_from_failures(
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return the first 127 failure only when the collected failures are all 127."""
    if not failures:
        return None
    if any(failure.returncode != 127 for failure in failures):
        return None
    return failures[0]


def _preferred_pre_push_failure(result: ValidationResult) -> ValidationCommandResult | None:
    """Return the failure that should drive diagnostics and repair prompts."""
    return _preferred_pre_push_failure_from_failures(
        result,
        _failed_pre_push_commands(result),
    )


def _preferred_pre_push_failure_from_failures(
    result: ValidationResult,
    failures: tuple[ValidationCommandResult, ...],
) -> ValidationCommandResult | None:
    """Return the preferred failure using an already collected failure tuple."""
    real_failure = _first_real_pre_push_failure_for_result(result, failures)
    if real_failure is not None:
        return real_failure
    toolchain_failure = _pure_toolchain_missing_failure_from_failures(failures)
    if toolchain_failure is not None:
        return toolchain_failure
    return result.first_failure


def _pre_push_validation_reason_code_for_preferred_failure(
    result: ValidationResult,
    preferred_failure: ValidationCommandResult | None,
) -> str:
    """Return the validation reason for an already selected preferred failure."""
    validation_reason_code = _validation_run_reason_code(result)
    if preferred_failure is None:
        return validation_reason_code
    coverage_command = result.coverage.command_result if result.coverage is not None else None
    # ValidationResult.first_failure returns this same coverage command object when
    # a coverage policy fails; preserve that identity if coverage results are copied.
    if (
        result.coverage is not None
        and not result.coverage.ok
        and preferred_failure is coverage_command
    ):
        return validation_reason_code
    return preferred_failure.reason_code


def _pre_push_validation_reason_code(result: ValidationResult) -> str:
    """Return the underlying validation reason, honoring mixed-failure precedence."""
    return _pre_push_validation_reason_code_for_preferred_failure(
        result,
        _preferred_pre_push_failure(result),
    )
