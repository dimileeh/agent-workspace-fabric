# Review PRRT_kwDOSJAM6s6F56Tj Processed Operator Hint Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6F56Tj_processed_operator_hint_PLAN.md`

## Requirement Status

- Reproduce the stale persist path where the database has a processed operator
  hint marker and no pending hint, while runtime state still has the same
  pending hint: Complete.
- Preserve the processed marker from the database when persisting stale runtime
  state: Complete.
- Do not re-persist `OPERATOR_HINT_STATE_KEY` for a hint that has already been
  marked processed: Complete.
- Keep unrelated monitor state updates from the stale loop, such as newly
  addressed review threads: Complete.
- Avoid broad AWF/GitHub-owned validation; run focused checks only: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/lifecycle.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`
- `plans/review_PRRT_kwDOSJAM6s6F56Tj_processed_operator_hint_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6F56Tj_processed_operator_hint_VALIDATION.md`

Focused checks:

- Before implementation, `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k processed_operator_hint_marker`
  failed because `OPERATOR_HINT_STATE_KEY` was resurrected in persisted monitor
  state.
- After implementation, `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k 'processed_operator_hint_marker or round_trips_pending_operator_hint'`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns that after
agent completion.
