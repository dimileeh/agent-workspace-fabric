# Merge Rejection Origin Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6L1IlT` reports that merge-rejection
preservation is inferred from the user-facing `awaiting_human_reason` text.
That makes machine behavior depend on a human-readable message.

Scope is limited to merge-block attention markers in
`src/awf/runtime/pr_monitor.py`,
`src/awf/runtime/pr_monitor_runner/merge_attention.py`, and focused tests.

## Requirements Checklist

- Store merge-rejection origin as structured monitor state instead of relying on
  `awaiting_human_reason` text for new markers.
- Persist the origin marker atomically with the existing merge-block marker and
  attention row update.
- Clear the origin marker whenever the merge-block marker is cleared.
- Preserve compatibility for already-persisted legacy merge-rejection markers
  that only have the prior reason text.
- Add focused regression coverage proving a changed human-readable reason still
  preserves merge-rejection attention when the structured origin flag is set.

## Implementation Steps

1. Add a reserved merge-block origin key/value and helper methods to
   `MonitorState`.
2. Mark deterministic merge API rejections with the structured origin when the
   merge loop stamps the merge-block attention marker.
3. Update durable marker persist/clear paths to include/remove the origin key.
4. Replace the primary string-based origin check with structured marker lookup,
   retaining a legacy fallback only for older rows without the origin marker.
5. Update focused merge-attention tests and run the narrow affected test subset.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_state.py tests/unit/runtime/test_pr_monitor_merge_attention.py -q`

Full AWF/GitHub validation is managed after agent completion and will not be run
inside this repair cycle.
