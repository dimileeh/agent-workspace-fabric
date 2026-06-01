# PRRT_kwDOSJAM6s6F-w1 Monitor Handoff Setup Failure Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F-w1` reports that monitor handoff profile setup command failures are not retried by the outer `_MonitorHandoffSetupFailureError` path when both the precise `_mark_failed` attempt and its generic fallback `_mark_failed` attempt fail. The current inner fallback handler logs the fallback failure and returns `False`, so callers treat the setup failure as handled even though no terminal workspace transition was persisted.

Scope is limited to `src/awf/control/executor/monitor_handoff_setup.py` and focused unit coverage for that helper path.

## Requirements Checklist

- Re-raise `_MonitorHandoffSetupFailureError` when the generic fallback mark-failed attempt also fails after a setup command failure.
- Preserve the existing fallback payload behavior: the outer retry should use `PR_MONITOR_SETUP_FAILED_REASON_CODE` without setup-dependency details after the dependency-specific attempt failed.
- Keep successful fallback behavior unchanged: if the generic fallback mark-failed attempt succeeds, return `False` after marking the workspace failed.
- Add/update a regression test that fails on the current swallowed fallback exception.
- Do not run broad AWF/GitHub-owned validation; use targeted unit tests only.

## Implementation Steps

1. Update the direct helper regression around fallback mark-failed failure so it expects `_MonitorHandoffSetupFailureError` instead of a local `False`.
2. Run the focused test to confirm the current implementation fails.
3. Re-raise from the inner fallback exception handler in `_run_monitor_handoff_profile_setup`.
4. Run targeted tests covering the direct helper and executor-level retry behavior.
5. Record validation evidence in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q`
  - Passes with the updated regression and existing executor-level retry coverage.

Full AWF/GitHub validation is intentionally left to AWF after this agent phase.
