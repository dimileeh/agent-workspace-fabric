# PRRT_kwDOSJAM6s6Kw8O2 Plan

## Problem Statement and Scope

The operator-hint repair path catches policy, runtime ownership, and mirror-hooks commit-sink failures from `_invoke_cli_for_verdict_result`, but review feedback reports that `_MonitorHeadObjectMissingError` can escape unhandled when `_commit_dirty_worktree` raises it during operator-hint repair.

Scope is limited to the operator-hint repair exception handling and a focused regression test.

## Requirements Checklist

- Verify whether `_MonitorHeadObjectMissingError` can be raised through `_invoke_cli_for_verdict_result`.
- Add focused regression coverage for operator-hint repair converting that exception into a failed `_GitPushResult`.
- Preserve existing operator-hint state handling by marking the pending hint as `needs_human` with the concrete failure reason.
- Use the established unrecoverable HEAD reason code used by other PR-monitor commit-sink callers.
- Run only focused validation for the changed behavior; AWF/GitHub own broad validation after agent completion.

## Implementation Steps

1. Add a unit test in `tests/unit/runtime/test_pr_monitor_operator_hints.py` that makes `_invoke_cli_for_verdict_result` raise `_MonitorHeadObjectMissingError`.
2. Confirm the new test fails against the current implementation when practical.
3. Import `_MonitorHeadObjectMissingError` and `_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON` in `operator_hints.py`.
4. Catch `_MonitorHeadObjectMissingError` in `_run_operator_hint_cycle`, mark the hint `needs_human`, and return a reason-coded failed `_GitPushResult`.
5. Re-run the focused test.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k head_object_missing`

Pass criteria: the focused regression test passes. Full AWF/GitHub validation is intentionally not run in the agent phase.
