# PRRT_kwDOSJAM6s6KLm5P Plan

## Problem Statement and Scope

The executor currently treats a mirror `core.hooksPath` repair exception before
profile setup as log-only. Monitor and pre-push paths hard-stop the same
condition, so the executor can continue into setup with a poisoned shared mirror
and later fail without the infrastructure reason code.

Scope is limited to `src/awf/control/executor/execution_flow.py` and focused
regression coverage for that setup-time failure path.

## Requirements Checklist

- Verify the review claim against the executor implementation.
- Add a focused regression test proving setup is not run when mirror hooks path
  repair raises before profile setup.
- Mark the workspace failed with an infrastructure failure and a reason code
  consistent with existing monitor/pre-push mirror hooks handling.
- Preserve existing behavior when no mirror path exists or repair succeeds.
- Run only targeted tests for the changed behavior; broad AWF/GitHub validation
  remains managed by AWF after agent completion.

## Implementation Steps

1. Add a direct `execution_flow.execute` regression with a minimal executor stub,
   patched mirror path lookup, and patched repair failure.
2. Update the executor setup path to log the mirror hooks reason code, call
   `_mark_failed`, and return before profile setup.
3. Run the new targeted test, then any narrow existing test file impacted by the
   direct executor setup path if needed.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`

Pass criteria: the new regression passes and shows `run_profile_phases` is not
called after a setup-time mirror hooks repair failure.
