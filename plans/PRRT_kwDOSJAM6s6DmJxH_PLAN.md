# PRRT_kwDOSJAM6s6DmJxH Plan

## Problem Statement and Scope

The preserved active-execution restart path must treat an in-progress
worker-restart rebase recovery as active salvage. The current lookup accepts
`recovery_mode="rebase_only"` only when the operation row type is `validate`,
so an active `rebase` operation can be missed during restart recovery.

Scope is limited to the worker preserved-salvage active operation lookup and
focused unit coverage for that lookup.

## Requirements Checklist

- Add a regression test proving `_has_active_preserved_validation_recovery`
  returns true for active worker-restart `rebase` operations with
  `recovery_mode="rebase_only"`.
- Keep existing validate-only and validate-row `rebase_only` recovery behavior.
- Preserve source, status, workspace, and salvage reason filtering so unrelated
  operations do not count as active preserved salvage.
- Run targeted unit validation for the changed worker tests.

## Implementation Steps

1. Add the failing regression test in `tests/unit/control/test_worker.py`.
2. Update `src/awf/control/worker.py` so the active preserved recovery lookup
   includes active `OperationType.rebase` rows with `recovery_mode="rebase_only"`.
3. Run the focused regression test, then the nearby worker lookup tests.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6DmJxH_VALIDATION.md`.
