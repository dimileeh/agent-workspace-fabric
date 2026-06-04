# PRRT_kwDOSJAM6s6HKbYM Empty-Plan Partial GC Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6HKbYM` reports that completed-workspace
filesystem cleanup returns early on partial GC results before running the
empty-plan auth-overlay unmount. When the empty-plan fallback compose teardown
succeeded but a later non-compose side effect, such as reservation release,
made the result partial, containers are already down and the auth overlay can be
left mounted.

Scope is limited to the PR monitor completion lifecycle path and focused
regression coverage for this partial empty-plan fallback case.

## Requirements Checklist

- Verify the reviewer claim against `src/awf/runtime/pr_monitor_runner/lifecycle.py`.
- Ensure successful empty-plan fallback compose teardowns unmount the auth
  overlay even when later side effects make the GC result partial.
- Preserve failed fallback compose teardown behavior: do not unmount the overlay
  when teardown failed because containers may still be alive.
- Keep logging semantics for partial GC results intact.
- Avoid broad validation; record focused checks only because AWF owns full
  validation after agent completion.

## Implementation Steps

1. Update focused monitor completion GC coverage for a successful empty-plan
   compose teardown with a partial result caused by reservation release.
2. Move or gate the empty-plan auth-overlay unmount so it runs before partial
   returns whenever the fallback teardown succeeded.
3. Run the focused regression test and a narrow nearby selection for empty-plan
   auth-overlay behavior.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6HKbYM_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q -k "empty_plan_auth_overlay"`
  - The successful partial empty-plan fallback unmounts the auth overlay.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q -k "empty_plan or auth_overlay"`
  - Nearby empty-plan/auth-overlay lifecycle behavior remains green.
- Full AWF/GitHub validation is intentionally not run in the agent phase.
