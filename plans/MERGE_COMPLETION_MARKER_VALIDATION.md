# Merge Completion Marker Validation

Plan reference: `plans/MERGE_COMPLETION_MARKER_PLAN.md`

## Requirement Status

- Complete: A successful squash merge with a blank GitHub merge SHA records a non-empty marker.
- Complete: A successful merge-commit merge with a blank GitHub merge SHA records a non-empty marker.
- Complete: Existing rebase fallback behavior remains unchanged.
- Complete: Targeted tests demonstrate the regression and the fix.
- Complete: Full AWF/GitHub validation remains delegated to AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `tests/unit/runtime/test_pr_monitor_merge_methods.py`
- `plans/MERGE_COMPLETION_MARKER_PLAN.md`
- `plans/MERGE_COMPLETION_MARKER_VALIDATION.md`

Focused checks run:

- Before production fix:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q -k 'non_rebase_merge_with_empty_merge_commit_records_head_marker'`
  - Result: failed for squash and merge because `workspace.pr_merge_sha` was `None`.
- After production fix:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  - Result: `23 passed in 31.07s`.
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_merge_methods.py`
  - Result: passed.

Full AWF/GitHub validation was not executed during the agent phase; AWF owns
the broad validation, provenance, logs, timeouts, and merge gates after agent
completion.
