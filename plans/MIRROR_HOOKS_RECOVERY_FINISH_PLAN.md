# Mirror Hooks Recovery Finish Plan

## Problem Statement And Scope

An inline PR review reported that executor recovery resumes can fail while
repairing a poisoned bare-mirror `core.hooksPath` before profile setup, but the
failure branch marks the workspace failed without first finishing the active
monitor recovery operation. Scope is limited to that mirror-hooks failure branch
and a focused regression test.

## Requirements Checklist

- Verify the mirror-hooks repair failure branch finishes active recovery
  operations when `recovery is not None`.
- Preserve the existing non-recovery failure behavior.
- Keep the failure reason code propagated to both recovery completion and
  workspace failure.
- Add a focused regression test for the recovery branch.
- Run only targeted checks for the changed behavior; full AWF/GitHub validation
  remains managed after agent completion.

## Implementation Steps

1. Extend the existing mirror-hooks executor regression coverage with an active
   recovery payload and assertions for `_finish_active_recovery_operations`.
2. Update `src/awf/control/executor/execution_flow.py` so the mirror repair
   failure branch mirrors adjacent setup/preflight branches.
3. Run the focused mirror-hooks test file.
4. Record validation evidence in a validation document.
