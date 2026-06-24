# Merge Rejection Origin Restamp Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6L1ZHt` reports that
`_clear_stale_merge_attention` can preserve a TTL-stale merge-rejection marker
but re-stamp it without the structured merge-rejection origin. The scope is
limited to preserving that origin during the critical-section re-stamp path and
covering the behavior with a focused regression test.

## Requirements Checklist

- Verify the review claim against the local `merge_attention.py` and
  `MonitorState` behavior.
- Add or update a focused regression test that fails when the structured
  merge-rejection origin is lost during stale critical-section preservation.
- Change only the re-stamp behavior needed for this review thread.
- Run focused tests for the changed behavior only. Full AWF/GitHub validation is
  managed after the agent phase.

## Implementation Steps

1. Update the existing critical-section merge-rejection preservation regression
   to assert the structured origin remains in memory and in the persisted row.
2. Pass `originated_from_merge_rejection=True` when the preserve branch is
   specifically preserving a merge-rejection-origin marker.
3. Run the targeted unit test for the regression.
4. Record validation evidence in
   `plans/MERGE_REJECTION_ORIGIN_RESTAMP_VALIDATION.md`.
