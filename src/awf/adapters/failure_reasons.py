"""Shared command-failure reason normalization for coding adapters."""

from __future__ import annotations

from awf.adapters.provider_failures import classify_provider_failure
from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    CommandResult,
)


def masked_agent_timeout_reason_code(exc: BaseException) -> str | None:
    """Agent-level watchdog timeout an escaping exception is masking, if any.

    ``run_streaming`` tags an exception that escapes *after* its watchdog already
    classified the run (``mark_masked_command_reason_code``). Translate that
    command-level classification into the agent vocabulary the timeout-preserve
    paths read, so the classification survives instead of reaching them as an
    ordinary failure (PRRT_kwDOSJAM6s6f7rCe).
    """
    reason_code = getattr(exc, "command_reason_code", None)
    if reason_code == COMMAND_TIMEOUT_REASON:
        return "AGENT_TIMEOUT"
    if reason_code == COMMAND_IDLE_TIMEOUT_REASON:
        return "AGENT_IDLE_TIMEOUT"
    return None


def _failure_reason_for_result(result: CommandResult) -> str:
    """Normalize command timeout/provider failure reason codes for retries."""
    if result.reason_code == COMMAND_TIMEOUT_REASON:
        return "AGENT_TIMEOUT"
    if result.reason_code == COMMAND_IDLE_TIMEOUT_REASON:
        return "AGENT_IDLE_TIMEOUT"
    provider_failure = classify_provider_failure(
        reason_code=None,
        stdout=result.stdout,
        stderr=result.stderr,
        provider=None,
        model=None,
    )
    if provider_failure is not None:
        return provider_failure.reason_code
    return "AGENT_CLI_FAILED"
