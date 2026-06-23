# Merge Attention CLEAN Queue Validation

Plan reference: `plans/MERGE_ATTENTION_CLEAN_QUEUE_PLAN.md`

## Requirement Status

- Preserve an existing merge-block attention marker and `awaiting_human_since` across queue-style waits when the observed GitHub merge-state signal is `CLEAN`: Complete.
- Continue treating `BLOCKED` and `HAS_HOOKS` as active branch-protection signals: Complete.
- Keep Bitbucket `CLEAN` conservative: Complete.
- Do not broaden validation beyond focused tests for the changed behavior: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/merge_attention.py`
- `tests/unit/runtime/test_pr_monitor_merge_attention.py`
- `plans/MERGE_ATTENTION_CLEAN_QUEUE_PLAN.md`
- `plans/MERGE_ATTENTION_CLEAN_QUEUE_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q -k "clean_status_preserves_merge_block_attention_during_queue_wait"`
  - Failed before the implementation change with the marker cleared from in-memory state.
  - Passed after the implementation change.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q -k "queue_wait or clear_stale_merge_attention_preserves_stale_marker_when_forge_still_blocked"`
  - Passed: 3 passed, 19 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  - Passed.

Full AWF/GitHub validation was not run in this agent phase, per the workspace contract; AWF owns broad validation and merge-gate provenance after completion.

## Gaps

None.
