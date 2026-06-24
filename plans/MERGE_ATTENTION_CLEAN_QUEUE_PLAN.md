# Merge Attention CLEAN Queue Plan

## Problem Statement and Scope

An unresolved PR review thread reports that queue, reviewer-settle, or initial-grace waits can clear an active `merge_block_attention` marker when GitHub still reports `CLEAN` from the status that preceded a deterministic merge rejection. That `CLEAN` signal is not proof that the branch-protection block has been retried and resolved.

Scope is limited to merge-block attention preservation in `src/awf/runtime/pr_monitor_runner/merge_attention.py` and focused regression coverage in `tests/unit/runtime/test_pr_monitor_merge_attention.py`.

## Requirements Checklist

- Preserve an existing merge-block attention marker and `awaiting_human_since` across queue-style waits when the observed GitHub merge-state signal is `CLEAN`.
- Continue treating `BLOCKED` and `HAS_HOOKS` as active branch-protection signals.
- Keep Bitbucket `CLEAN` conservative.
- Do not broaden validation beyond focused tests for the changed behavior; AWF/GitHub own broad validation after agent completion.

## Implementation Steps

1. Add a focused regression test proving queue-wait handling preserves a prior merge-block marker and attention flag when status is GitHub `CLEAN`.
2. Run that single test and confirm it fails against the current implementation.
3. Update merge-attention queue verdict classification so `CLEAN` is indeterminate for queue-style waits and therefore preserves markers until a merge retry path confirms resolution.
4. Update nearby documentation/comments to match the new preservation contract.
5. Re-run the focused regression and a narrow related merge-attention test selection.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q -k "clean_status_preserves_merge_block_attention_during_queue_wait"`
  - Passes after the implementation change.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q -k "queue_wait or clear_stale_merge_attention_preserves_stale_marker_when_forge_still_blocked"`
  - Passes for related queue/attention coverage.

Full AWF/GitHub validation is intentionally not run during this agent phase.
