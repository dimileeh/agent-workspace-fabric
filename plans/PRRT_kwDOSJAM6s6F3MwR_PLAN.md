# PRRT_kwDOSJAM6s6F3MwR Plan

## Problem Statement And Scope

The review thread reports that `test_custom_plan_artifact_overlap_does_not_block_later_candidate`
asserts unsupported broad-glob behavior for custom profile planning artifacts. The production
owned-path classifier treats concrete configured custom artifact files as internal plan artifacts,
while keeping parent scopes such as `docs/alternate/**` as real interworkspace-owned paths.

Scope is limited to correcting the merge-queue regression test for this review thread.

## Requirements Checklist

- Confirm the targeted test fails for the reported reason before changing behavior.
- Preserve production owned-path filtering semantics.
- Update only the affected regression so it uses concrete custom plan artifact paths.
- Run focused verification for the changed test.
- Do not run broad AWF/GitHub-owned validation.
- Commit the local fix on the current AWF-managed branch.

## Implementation Steps

1. Inspect the affected test and owned-path filtering helpers.
2. Make workspace IDs deterministic inside the affected test.
3. Replace the custom profile parent glob ownership with concrete generated plan artifact files.
4. Re-run the focused test.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6F3MwR_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_custom_plan_artifact_overlap_does_not_block_later_candidate -q`
  - Passes after the test is corrected.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
