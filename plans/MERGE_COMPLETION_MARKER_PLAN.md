# Merge Completion Marker Plan

## Problem Statement

PR monitor merge completion can record an empty merge marker after a successful
squash or merge-commit merge when GitHub returns a blank follow-up SHA. The
lifecycle completion path only persists truthy `pr_merge_sha` values, so a
merged workspace can complete without a merge marker.

## Scope

- Keep the fix limited to PR monitor merge marker normalization.
- Preserve the existing rebase behavior that records the PR head SHA when no
  merge commit SHA exists.
- Add focused regression coverage for successful non-rebase merges with blank
  merge SHA responses.

## Requirements Checklist

- [ ] A successful squash merge with a blank GitHub merge SHA records a non-empty marker.
- [ ] A successful merge-commit merge with a blank GitHub merge SHA records a non-empty marker.
- [ ] Existing rebase fallback behavior remains unchanged.
- [ ] Targeted tests demonstrate the regression and the fix.
- [ ] Full AWF/GitHub validation remains delegated to AWF after agent completion.

## Implementation Steps

1. Add a focused regression test in `tests/unit/runtime/test_pr_monitor_merge_methods.py`
   that exercises blank merge SHA responses for squash and merge methods.
2. Run the focused test and confirm it fails before changing production code.
3. Update `_merge_completion_marker` in
   `src/awf/runtime/pr_monitor_runner/merge_loop.py` to normalize blank SHA
   responses to the PR head SHA for all successful merge methods.
4. Run the targeted merge-method tests affected by the change.
5. Record validation evidence in
   `plans/MERGE_COMPLETION_MARKER_VALIDATION.md`.
