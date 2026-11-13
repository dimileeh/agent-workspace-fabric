"""CI-failure classification primitives for the PR-monitor decision core.

Extracted from :mod:`awf.runtime.pr_monitor` so that module stays within the
first-party file-line guardrail
(``tests/unit/test_core_decomposition_maintainability.py``). Behavior is
unchanged: these are pure predicates over :class:`CheckFailure` / :class:`PRStatus`
wire shapes with no dependency on ``MonitorState`` or ``MonitorConfig``, and the
symbols are re-exported from ``pr_monitor`` for existing import sites.
"""

from __future__ import annotations

import re

from awf.runtime._docker_pull_detection import _log_shows_docker_registry_timeout
from awf.runtime.pr_monitor_models import (
    CheckFailure,
    PRStatus,
)

_CI_FAILED_JOB_RERUN_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT"})

_CI_TRANSIENT_FAILURE_MARKERS = (
    "timed_out",
    "http status server error",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "500 internal server",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "service unavailable",
    "temporarily unavailable",
    "try again",
    "timed out waiting for",
    "timeout awaiting",
    "connection reset",
    "connection refused",
    "connection aborted",
    "recv failure",
    "tls handshake timeout",
    "failed to download",
    "network is unreachable",
    "runner has received a shutdown signal",
    "lost communication with the server",
)

_CI_CODE_FAILURE_MARKERS = (
    "would reformat:",
    "would be reformatted",
    "fail-under",
    "coverage failure",
    "required test coverage",
    "coverage below required threshold",
    "traceback (most recent call last):",
    "assertionerror",
    "assertionfailederror",
    "=== short test summary info ===",
    "found type errors",
    "found lint errors",
    "syntaxerror",
    "expect(received)",
    "test result: failed",
    "panicked at",
    "--- fail:",
    "panic:",
)

_RUFF_DIAGNOSTIC_RE = re.compile(r"\S+\.py:\d+:\d+:\s+[a-z]{1,4}\d{3,4}\b", re.IGNORECASE)
_LINT_TOOL_FAILURE_RE = re.compile(
    r"\b(?:ruff|mypy|eslint)\b[^\n]*\b(?:failed|found|would reformat|errors?)\b",
    re.IGNORECASE,
)


def _log_shows_code_failure(log_text: str) -> bool:
    """Return whether CI log text contains markers of a code-level test or lint failure."""
    if any(marker in log_text for marker in _CI_CODE_FAILURE_MARKERS):
        return True
    if _LINT_TOOL_FAILURE_RE.search(log_text):
        return True
    for line in log_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("failed ") and "::" in stripped:
            return True
        if ".py:" in stripped and ": error:" in stripped:
            return True
        if _RUFF_DIAGNOSTIC_RE.search(stripped):
            return True
    return False


def _failure_has_parsed_code_evidence(failure: CheckFailure) -> bool:
    """Return whether structured CI failure evidence points to a code defect."""
    if failure.test_node_ids or failure.assertion_snippets:
        return True
    if failure.error_summaries:
        return _log_shows_code_failure("\n".join(failure.error_summaries).lower())
    return False


def _failure_has_actionable_ci_evidence(failure: CheckFailure) -> bool:
    """Return whether a CI failure row gives the repair agent something to work with."""
    if failure.log_excerpt.strip():
        return True
    if _failure_has_parsed_code_evidence(failure):
        return True
    return bool(failure.evidence_warnings)


def _ci_failure_identity(failure: CheckFailure) -> tuple[str, str, str]:
    """Stable identity for one failing check inside a retry-budget key.

    When a workflow ``run_id`` is available it *is* the stable identity for the
    failing run, so the free-form check ``name`` and ``conclusion`` are dropped:
    the same persistent run can otherwise present different names/conclusions
    across polls (e.g. one poll records the workflow run name from
    ``gh run list``, while a fallback poll records the rollup check name and a
    defaulted conclusion). Keying on those drifting fields would mint a fresh
    key for the same failure and silently reset the rerun/infra-wait budget,
    granting extra reruns past ``ci_transient_rerun_max_attempts``. Without a
    ``run_id`` we fall back to the name/conclusion pair as the only identity.
    """

    if failure.run_id:
        return (failure.run_id, "", "")
    return ("", failure.name, failure.conclusion)


def _looks_like_transient_ci_failure(failure: CheckFailure) -> bool:
    """True for retryable infrastructure flakes."""

    if _failure_has_parsed_code_evidence(failure):
        return False
    log_text = failure.log_excerpt.lower()
    if not log_text.strip():
        return bool(failure.run_id) and failure.conclusion.upper() == "TIMED_OUT"
    shows_code_failure = _log_shows_code_failure(log_text)
    if not shows_code_failure and any(
        marker in log_text for marker in _CI_TRANSIENT_FAILURE_MARKERS
    ):
        return True
    return not shows_code_failure and _log_shows_docker_registry_timeout(log_text)


def _ci_failure_is_rerun_candidate(failure: CheckFailure) -> bool:
    """Return whether a failure can participate in transient CI rerun/wait logic."""
    return bool(failure.run_id)


def _ci_candidate_failures(status: PRStatus) -> tuple[CheckFailure, ...]:
    """Return CI failures eligible for transient rerun/wait.

    Synthesized rollup/status rows (no ``run_id``) are kept on ``status.ci_failures``
    for reporting but excluded here so they do not block rerunning retryable Actions
    failures on the same PR head.
    """
    return tuple(
        failure for failure in status.ci_failures if _ci_failure_is_rerun_candidate(failure)
    )


def _ci_non_candidate_failures(status: PRStatus) -> tuple[CheckFailure, ...]:
    """Return CI failures excluded from transient rerun/wait (no ``run_id``)."""
    return tuple(
        failure for failure in status.ci_failures if not _ci_failure_is_rerun_candidate(failure)
    )


def _non_candidate_carries_fixable_code_evidence(status: PRStatus) -> bool:
    """True when a synthesized/external row still carries repairable code evidence."""
    for failure in _ci_non_candidate_failures(status):
        if _failure_has_parsed_code_evidence(failure):
            return True
        if _log_shows_code_failure(failure.log_excerpt.lower()):
            return True
    return False
