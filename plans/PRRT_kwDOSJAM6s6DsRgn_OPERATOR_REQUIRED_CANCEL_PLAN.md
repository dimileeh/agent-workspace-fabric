# PRRT_kwDOSJAM6s6DsRgn Operator-Required Cancellation Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6DsRgn` reports that preserved active execution
recovery leaves stale pre-restart validate/push operations active when a
`running` workspace is marked `OPERATOR_REQUIRED`. The current operator-required
salvage writer only cancels superseded active operations for non-running
candidates.

## Scope

- Inspect the operator-required salvage path in `src/awf/control/worker.py`.
- Add regression coverage proving a `running` workspace entering
  operator-required recovery cancels the stale pending validate operation.
- Make the smallest behavior change needed to cancel superseded active
  execution operations for this path.

## Requirements Checklist

- A running preserved-active workspace with a stale pending validate operation
  must cancel that operation when operator recovery is recorded.
- Existing non-running cancellation behavior must remain covered.
- Operator-required payload and event payload must include the cancelled
  operation details.
- Fresh execution claims must still prevent salvage writers from mutating the
  workspace.

## Implementation Steps

1. Extend the existing operator-required cancellation regression test with the
   running/pending-validate case and confirm it fails before implementation.
2. Update `_record_preserved_active_operator_required` so it invokes
   `_cancel_superseded_active_execution_operations` for the running path too.
3. Run the narrow affected test, then run the focused worker test surface if
   practical.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_operator_required_cancels_superseded_active_operation or preserved_active_salvage_writers_recheck_fresh_execution_claim'`

Pass criteria: the targeted tests pass, and the validation document records the
evidence and any remaining gaps.
