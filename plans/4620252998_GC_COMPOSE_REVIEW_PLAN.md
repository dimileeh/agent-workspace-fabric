# Review Comment 4620252998 GC Compose Plan

## Problem Statement and Scope

Address the review-level feedback on PR #396 about compose teardown fallback
metadata and lifecycle tracking for completed workspace GC. Keep the change
limited to the reviewed GC/lifecycle behavior and focused regression tests.

## Requirements Checklist

- Verify the preserved-workspace compose teardown fallback uses one clear
  extension surface instead of a separate hard-coded completed workspace branch.
- Ensure the lifecycle compose teardown tracking wrapper records an observable
  failed teardown when the wrapped callback raises before returning a result.
- Preserve the safety invariant that auth-overlay unmount only happens after an
  observed successful compose teardown.
- Add or update focused tests for the changed behavior.
- Do not run AWF/GitHub-owned broad validation; record that AWF owns full
  validation after agent completion.

## Implementation Steps

1. Add focused failing tests for the preserved fallback extension surface and
   callback-exception tracking behavior.
2. Update `src/awf/service/gc.py` so preserved fallback eligibility is driven by
   a single status/reason extension table.
3. Update `src/awf/runtime/pr_monitor_runner/lifecycle.py` so the tracking
   wrapper records a `COMPOSE_TEARDOWN_CALLBACK_RAISED` failure before
   re-raising callback exceptions.
4. Run the focused unit tests that exercise the touched behavior.
5. Save validation evidence in
   `plans/4620252998_GC_COMPOSE_REVIEW_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py -q -k "preserved_completed_workspace_compose_teardown_fallback or non_completed_workspace_within_retention_skips_fallback_compose"`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q -k "callback_raises or raises_after_teardown"`

Pass criteria: all focused tests pass. Full AWF/GitHub validation is managed by
AWF after agent completion per the workspace contract.
