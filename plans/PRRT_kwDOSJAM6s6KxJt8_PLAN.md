# PRRT_kwDOSJAM6s6KxJt8 Plan

## Problem Statement and Scope

The PR review thread reports that `remote_repair._commit_dirty_worktree` catches a bare
`Exception` from `repair_mirror_hooks_path`, logs only the generic poisoned-hooks
reason, and raises `_MonitorMirrorHooksPathRepairFailedError` with `from None`.
This hides the underlying mirror repair failure.

Scope is limited to the mirror hooks-path repair failure handling in
`src/awf/runtime/pr_monitor_runner/remote_repair.py` and a focused regression test.

## Requirements Checklist

- Verify the current code still catches broad exceptions and suppresses the cause.
- Catch only expected mirror repair failures from `repair_mirror_hooks_path`.
- Preserve underlying exception details in structured warning logs.
- Raise `_MonitorMirrorHooksPathRepairFailedError` with the original exception as the cause.
- Add focused regression coverage for this handler.
- Do not run broad AWF/GitHub-owned validation; record only targeted local checks.

## Implementation Steps

1. Add a focused failing test for `GitOperationError` from mirror hooks-path repair.
2. Import the expected exception type in `remote_repair.py`.
3. Replace the bare `Exception` handler with expected exception types and structured logging.
4. Raise the typed monitor error using `from exc`.
5. Run the targeted regression test only.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q -k mirror_hooks`
  - Passes with the new regression included.
- Full AWF/GitHub validation is intentionally left to AWF after agent completion.
