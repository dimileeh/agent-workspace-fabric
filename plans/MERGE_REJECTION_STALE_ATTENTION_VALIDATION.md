# Merge Rejection Stale Attention Validation

Plan reference: `plans/MERGE_REJECTION_STALE_ATTENTION_PLAN.md`

## Requirement Status

- Add a failing regression for a TTL-stale merge-block marker whose persisted
  `awaiting_human_reason` says GitHub rejected the merge attempt while status is
  `CLEAN`: Complete.
- Preserve and durably re-stamp that marker at merge critical-section entry:
  Complete.
- Keep existing stale non-rejection behavior: ordinary resolved markers still
  clear attention and the marker: Complete.
- Run only focused tests for the touched behavior: Complete.
- Record validation evidence in this document: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/merge_attention.py`
- `tests/unit/runtime/test_pr_monitor_merge_attention.py`
- `plans/MERGE_REJECTION_STALE_ATTENTION_PLAN.md`
- `plans/MERGE_REJECTION_STALE_ATTENTION_VALIDATION.md`

Focused checks run:

- Failing-before regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q -k github_clean_status_preserves_stale_merge_rejection_attention_at_critical_section`
  failed with `KeyError: '__awf_merge_block_attention__'`, confirming the marker
  was cleared before the fix.
- Passing focused regression set:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q -k "github_clean_status_preserves_stale_merge_rejection_attention_at_critical_section or clear_stale_merge_attention_drops_marker_durably_on_resolve or clear_stale_merge_attention_preserves_stale_marker_when_forge_still_blocked or github_clean_status_clears_non_rejection_attention_during_queue_wait"`
  passed: `4 passed, 18 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion.

## Gaps

None.
