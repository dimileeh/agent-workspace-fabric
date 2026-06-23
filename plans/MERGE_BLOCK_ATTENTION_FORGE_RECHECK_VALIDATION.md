# Validation: Forge Re-check for Merge-block Attention (#671)

Plan reference: `plans/MERGE_BLOCK_ATTENTION_FORGE_RECHECK_PLAN.md`

## Requirement Status

- Complete: Queue/reviewer/initial-grace waits decide `merge_block_attention` from forge mergeability status instead of marker age.
- Complete: Forge `BLOCKED` / `HAS_HOOKS` preserves the marker and stable `awaiting_human_since` without queue-wait re-stamping.
- Complete: Forge `CLEAN` clears the marker and `awaiting_human_since` promptly while still waiting on the non-human gate.
- Complete: Indeterminate or failed targeted re-check preserves conservatively.
- Complete: The #666 `allow_age_out=False` queue preserve/re-stamp branch was removed; `_clear_stale_merge_attention` remains scoped to the #661 merge critical-section TTL path.
- Complete: #661 critical-section behavior remains covered by focused merge-attention tests.

## Files Changed

- `src/awf/runtime/pr_monitor.py`
- `src/awf/runtime/pr_monitor_runner/gates.py`
- `src/awf/runtime/pr_monitor_runner/merge_attention.py`
- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `src/awf/runtime/pr_monitor_runner/mixins.py`
- `tests/unit/runtime/test_merge_queue_ordering.py`
- `tests/unit/runtime/test_pr_monitor_merge_attention.py`

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
  - Passed: `40 passed in 63.34s`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/merge_loop.py src/awf/runtime/pr_monitor_runner/gates.py src/awf/runtime/pr_monitor_runner/mixins.py tests/unit/runtime/test_merge_queue_ordering.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  - Passed: `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/merge_loop.py src/awf/runtime/pr_monitor_runner/gates.py src/awf/runtime/pr_monitor_runner/mixins.py`
  - Passed: `Success: no issues found in 5 source files`

Full AWF/GitHub validation, full coverage, and CI-equivalent gates were not run inside the agent phase per the workspace contract; AWF owns those after completion.
