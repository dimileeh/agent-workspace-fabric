# PRRT_kwDOSJAM6s6L2Znf Plan

## Problem Statement and Scope

The review thread reports that `_clear_stale_merge_attention` can drop a
persisted merge-rejection origin when refreshing an existing
`merge_block_attention` marker. The helper already consults persisted origin
for stale/non-active markers, but not for fresh markers or markers preserved
because the forge still reports branch protection active.

Scope is limited to the merge-block attention refresh path and a focused
regression test.

## Requirements

- Verify the refresh path preserves merge-rejection origin when the origin is
  present only in the workspace row.
- Keep the current in-memory explicit-origin precedence behavior.
- Do not change queue-wait clearing semantics or unrelated monitor behavior.
- Run only focused tests for the changed behavior; broad AWF/GitHub validation
  remains owned by AWF after agent completion.

## Implementation Steps

1. Add a failing regression test for `_clear_stale_merge_attention` covering a
   marker with no in-memory origin and persisted merge-rejection origin.
2. Update the refresh path to reuse the DB-backed origin lookup when the marker
   is going to be preserved and refreshed.
3. Run the focused regression test, then the relevant focused merge-attention
   test file if practical.
4. Record results in `plans/PRRT_kwDOSJAM6s6L2Znf_VALIDATION.md`.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q -k "critical_section_refresh_preserves_persisted_merge_rejection_origin"`
- Optional focused file check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
