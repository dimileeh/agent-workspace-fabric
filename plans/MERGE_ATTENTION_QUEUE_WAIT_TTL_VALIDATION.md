# Merge Attention Queue Wait TTL Validation

Plan reference: `plans/MERGE_ATTENTION_QUEUE_WAIT_TTL_PLAN.md`

## Requirement Status

- Verify the review claim against current code: Complete.
  - Evidence: `merge_loop.py` passed only the entry timestamp into
    `_clear_stale_merge_attention`, while `merge_attention.py` applied TTL
    freshness without considering the current active forge status.
- Add a focused regression for active forge status plus TTL-stale marker:
  Complete.
  - Evidence:
    `tests/unit/runtime/test_pr_monitor_merge_attention.py::test_clear_stale_merge_attention_preserves_stale_marker_when_forge_still_blocked`.
  - The regression failed before implementation with
    `TypeError: _clear_stale_merge_attention() got an unexpected keyword argument 'status'`.
- Keep existing stale-marker cleanup behavior for resolved or unproven blocks:
  Complete.
  - Evidence: existing merge-attention tests still pass.
- Keep the fix minimal and scoped to merge attention / merge loop: Complete.
  - Evidence: changed only merge attention helper, merge-loop call site, focused
    regression test, and required plan/validation docs.
- Run focused tests only: Complete.
  - Evidence: commands below.
- Commit locally with a conventional commit referencing the review thread:
  Complete.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py::test_clear_stale_merge_attention_preserves_stale_marker_when_forge_still_blocked -q`
  - Result before implementation: failed as expected.
  - Result after implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
  - Result: `21 passed in 34.17s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  - Result: `All checks passed!`

Full AWF/GitHub validation, full coverage gates, and CI-equivalent commands were
not run inside this agent phase per the AWF workspace contract.
