# PRRT_kwDOSJAM6s6F7NSi Operator Hint Processed Load Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6F7NSi` reports that `_load_state()` restores a
persisted `OPERATOR_HINT_STATE_KEY` as `pending_operator_hint` even when the
same persisted monitor state already contains the matching processed marker.
That can make `decide()` dispatch a duplicate operator-hint repair pass after
the hint was already processed.

Scope is limited to monitor state loading for processed operator hints plus a
focused regression test.

## Requirements Checklist

- Preserve processed operator hint markers in runtime state.
- Do not restore `pending_operator_hint` when its `operation_id` has a
  matching processed marker.
- Keep terminal, unprocessed, and operation-id-less hint behavior unchanged.
- Add a focused regression test that fails before the loader fix.
- Run only targeted validation; broad AWF/GitHub validation remains owned by
  AWF after agent completion.

## Implementation Steps

1. Add a focused unit regression for loading persisted state with both a
   pending hint payload and its processed marker.
2. Confirm the new test fails before changing production code.
3. Update `_load_state()` to clear the parsed pending hint when the matching
   processed marker is present.
4. Re-run the focused regression.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6F7NSi_OPERATOR_HINT_PROCESSED_LOAD_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_load_state_ignores_processed_pending_operator_hint -q`
  - Before implementation: fails because `pending_operator_hint` is restored.
  - After implementation: passes and confirms merge is not blocked by the
    processed stale hint.
