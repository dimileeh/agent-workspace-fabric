# Review PRRT_kwDOSJAM6s6DxZ5k Replacement Attempt Blocked Plan

## Problem statement and scope

The preserved active execution replacement path can find an idempotently-created
replacement workspace that has no task attempt. The current branch only logs a
warning and returns `True`, leaving no salvage event for future scans or
operators. Scope is limited to recording clear salvage-blocked provenance for
that existing-replacement/missing-attempt state.

## Requirements checklist

- Add or update a regression test for an existing replacement workspace without
  a replacement attempt.
- Record exactly one `workspace.active_execution_salvage_blocked` event for the
  current preservation cycle with `blocked_reason="replacement_attempt_missing"`.
- Keep replacement-created salvage events absent until a replacement attempt
  exists.
- Preserve the existing warning signal with accurate salvage reason context.
- Avoid broad changes to unrelated stale-active recovery behavior.

## Implementation steps

1. Update the focused unit test around preserved active replacement missing
   attempts to expect a salvage-blocked event and idempotent repeat behavior.
2. Run the focused test to confirm it fails against current behavior.
3. Update `ControlWorker._create_preserved_active_replacement` to write the
   blocked event before returning when the existing replacement has no attempt.
4. Re-run the focused test and then the narrow worker unit subset needed for
   confidence.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k preserved_active_replacement_missing_existing_attempt`
  must fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_replacement"` must pass after implementation.
