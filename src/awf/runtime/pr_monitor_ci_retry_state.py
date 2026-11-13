"""CI transient-failure retry bookkeeping for the PR-monitor decision core.

Extracted from :mod:`awf.runtime.pr_monitor` so that module stays within the
first-party file-line guardrail
(``tests/unit/test_core_decomposition_maintainability.py``). Behavior is
unchanged: the retry/wait budgets live in ``MonitorState.threads_addressed_ids``
under signature keys, and the symbols are re-exported from ``pr_monitor`` for
existing import sites.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING

from awf.runtime.pr_monitor_ci_classification import _ci_failure_identity
from awf.runtime.pr_monitor_models import CheckFailure, PRStatus

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorConfig, MonitorState


_CI_TRANSIENT_RERUN_KEY_PREFIX = "__awf_ci_rerun:"
_CI_TRANSIENT_INFRA_WAIT_KEY_PREFIX = "__awf_ci_infra_wait:"


def _ci_transient_rerun_state_key(
    head_sha: str,
    failures: tuple[CheckFailure, ...],
) -> str:
    """Stable retry-budget key for one PR head/failing-run signature.

    The key deliberately excludes free-form log text. A rerun can produce
    slightly different infrastructure wording while still representing the
    same failing workflow run on the same PR head.
    """

    signature = json.dumps(
        sorted(_ci_failure_identity(failure) for failure in failures),
        separators=(",", ":"),
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    return f"{_CI_TRANSIENT_RERUN_KEY_PREFIX}{head_sha}:{digest}"


def _ci_transient_rerun_count(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
    legacy_failures: tuple[CheckFailure, ...] | None = None,
) -> int:
    keys = [_ci_transient_rerun_state_key(head_sha, failures)]
    if legacy_failures is not None and legacy_failures != failures:
        keys.append(_ci_transient_rerun_state_key(head_sha, legacy_failures))
    return max(_ci_transient_rerun_count_for_key(state, key) for key in keys)


def _ci_transient_rerun_count_for_key(state: MonitorState, key: str) -> int:
    raw_count = state.threads_addressed_ids.get(key, "0")
    try:
        return int(raw_count)
    except ValueError:
        return 0


def _ci_transient_infra_wait_state_key(
    head_sha: str,
    failures: tuple[CheckFailure, ...],
) -> str:
    rerun_key = _ci_transient_rerun_state_key(head_sha, failures)
    return rerun_key.replace(
        _CI_TRANSIENT_RERUN_KEY_PREFIX,
        _CI_TRANSIENT_INFRA_WAIT_KEY_PREFIX,
        1,
    )


_CI_REPAIR_ATTEMPTED_KEY_PREFIX = "__awf_ci_repair_attempted:"


def _ci_repair_attempted_state_key(head_sha: str, failures: tuple[CheckFailure, ...]) -> str:
    """Marker that a CI repair already ran for this head/failing-run signature (#1216)."""
    rerun_key = _ci_transient_rerun_state_key(head_sha, failures)
    return rerun_key.replace(_CI_TRANSIENT_RERUN_KEY_PREFIX, _CI_REPAIR_ATTEMPTED_KEY_PREFIX, 1)


def _ci_transient_infra_wait_started_key(head_sha: str, failures: tuple[CheckFailure, ...]) -> str:
    return f"{_ci_transient_infra_wait_state_key(head_sha, failures)}:started"


def _ci_transient_infra_wait_count_key(head_sha: str, failures: tuple[CheckFailure, ...]) -> str:
    return f"{_ci_transient_infra_wait_state_key(head_sha, failures)}:count"


def _ci_transient_infra_wait_count(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
) -> int:
    raw_count = state.threads_addressed_ids.get(
        _ci_transient_infra_wait_count_key(head_sha, failures),
        "0",
    )
    try:
        return int(raw_count)
    except ValueError:
        return 0


def _ci_transient_infra_wait_started_at(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
) -> float | None:
    raw_started = state.threads_addressed_ids.get(
        _ci_transient_infra_wait_started_key(head_sha, failures)
    )
    if raw_started is None:
        return None
    try:
        return float(raw_started)
    except ValueError:
        return None


def _record_ci_transient_infra_wait(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
    now: float | None = None,
) -> tuple[int, float]:
    current_time = time.time() if now is None else now
    started_key = _ci_transient_infra_wait_started_key(head_sha, failures)
    count_key = _ci_transient_infra_wait_count_key(head_sha, failures)
    started_at = _ci_transient_infra_wait_started_at(
        state,
        head_sha=head_sha,
        failures=failures,
    )
    if started_at is None:
        started_at = current_time
        state.threads_addressed_ids[started_key] = f"{started_at:.6f}"
    wait_count = _ci_transient_infra_wait_count(
        state,
        head_sha=head_sha,
        failures=failures,
    )
    recorded_count = wait_count + 1
    state.threads_addressed_ids[count_key] = str(recorded_count)
    return recorded_count, started_at


def _ci_transient_infra_wait_exhausted(
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
    failures: tuple[CheckFailure, ...],
) -> bool:
    max_seconds = config.ci_transient_infra_wait_max_seconds
    if max_seconds <= 0:
        return True
    started_at = _ci_transient_infra_wait_started_at(
        state,
        head_sha=status.head_sha,
        failures=failures,
    )
    return started_at is not None and time.time() - started_at >= max_seconds


def _ci_transient_infra_wait_seconds(
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
    failures: tuple[CheckFailure, ...],
) -> float:
    """Return exponential backoff seconds before retrying transient CI infra failures."""
    wait_count = _ci_transient_infra_wait_count(
        state,
        head_sha=status.head_sha,
        failures=failures,
    )
    exponent = min(max(wait_count, 0), 30)
    wait_seconds = max(config.poll_interval_seconds, 0.0) * float(2**exponent)
    cap = max(config.ci_transient_infra_wait_max_backoff_seconds, 0.0)
    if cap <= 0:
        return wait_seconds
    return wait_seconds if wait_seconds < cap else cap
