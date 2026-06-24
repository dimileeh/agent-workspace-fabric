# Merge Rejection Stale Attention Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6L1FjH` reports that
`_clear_stale_merge_attention` can clear a TTL-stale merge-block marker that
originated from a deterministic merge rejection even when GitHub reports
`CLEAN`. Actor/push restrictions can be invisible to `mergeStateStatus`, so the
critical-section entry path should preserve that attention until the merge retry
confirms success or re-stamps the rejection.

Scope is limited to `src/awf/runtime/pr_monitor_runner/merge_attention.py` and
focused regression coverage in `tests/unit/runtime/test_pr_monitor_merge_attention.py`.

## Requirements Checklist

- Add a failing regression for a TTL-stale merge-block marker whose persisted
  `awaiting_human_reason` says GitHub rejected the merge attempt while status is
  `CLEAN`.
- Preserve and durably re-stamp that marker at merge critical-section entry.
- Keep existing stale non-rejection behavior: ordinary resolved markers still
  clear attention and the marker.
- Run only focused tests for the touched behavior.
- Record validation evidence in a matching validation document.

## Implementation Steps

1. Add the focused regression test first and confirm it fails.
2. Update `_clear_stale_merge_attention` to apply the merge-rejection-origin
   preservation check before the stale clear.
3. Re-run the focused regression and nearby merge-attention tests that cover the
   stale clear/preserve contract.
4. Create `plans/MERGE_REJECTION_STALE_ATTENTION_VALIDATION.md` with requirement
   status and evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q -k "github_clean_status_preserves_stale_merge_rejection_attention_at_critical_section or clear_stale_merge_attention_drops_marker_durably_on_resolve or clear_stale_merge_attention_preserves_stale_marker_when_forge_still_blocked or github_clean_status_clears_non_rejection_attention_during_queue_wait"`

Pass criteria: the new regression and nearby stale clear/preserve tests pass.
Full AWF/GitHub validation is managed by AWF after agent completion.
