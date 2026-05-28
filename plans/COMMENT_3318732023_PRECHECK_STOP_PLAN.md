# Comment 3318732023 Precheck Stop Plan

## Problem Statement And Scope

The PR review thread `PRRT_kwDOSJAM6s6FcAIj` reports that `resume_pr_monitor`
records `MONITOR_RECOVERY_PRECHECK_FAILED` for a missing or empty required
companion env secret, then continues into PR monitor construction and
`monitor.run`. That can monitor an unrecovered runtime stack even though a
required credential is unavailable.

Scope is limited to the companion env secret precheck failure branch in
`src/awf/control/executor/monitor_handoff.py` and its focused unit coverage.
Ordinary compose restart failures should continue to use their existing
warning-and-monitor behavior.

## Requirements Checklist

- Required companion env precheck failures record the existing
  `MONITOR_RECOVERY_PRECHECK_FAILED` runtime restart event.
- Required companion env precheck failures stop monitor resume before monitor
  factory construction or `monitor.run`.
- Existing compose restart failure behavior remains unchanged.
- Add or update focused regression coverage for the stopped precheck path.
- Run only targeted local validation; AWF/GitHub owns broad validation after
  agent completion.

## Implementation Steps

1. Update the existing required companion env secret resume test to expect no
   monitor run after the precheck failure while preserving event assertions.
2. Run that focused test to confirm it fails against the current behavior.
3. Add an early return after recording the precheck failure.
4. Re-run the focused test.
5. Write `plans/COMMENT_3318732023_PRECHECK_STOP_VALIDATION.md` with
   requirement status and evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_010.py::TestExecutorCoverageEdgesPart010::test_resume_pr_monitor_preserves_required_companion_env_secret_reason_code -q`
  - Initially fails before implementation because the monitor still runs.
  - Passes after implementation for both missing and empty source cases.

Broad test suites, full coverage gates, frontend builds, and CI-equivalent AWF
validation are intentionally not run inside the agent phase.
