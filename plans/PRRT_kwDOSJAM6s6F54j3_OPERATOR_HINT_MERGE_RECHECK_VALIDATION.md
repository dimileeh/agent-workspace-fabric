# PRRT_kwDOSJAM6s6F54j3 Operator Hint Merge Recheck Validation

Plan reference: `PRRT_kwDOSJAM6s6F54j3_OPERATOR_HINT_MERGE_RECHECK_PLAN.md`

## Requirement Status

- Complete: Persisted operator hint state is rechecked inside the merge critical
  section before `merge_pr` is attempted.
- Complete: A newly discovered pending operator hint routes to the existing
  `AddressOperatorHint` action instead of merging.
- Complete: The refresh helper imports only the DB operator hint into the
  current `MonitorState`, preserving in-memory reviewer-settle and feedback
  markers.
- Complete: Added a regression for a stale `Merge` action with a concurrently
  persisted DB operator hint.
- Complete: Focused local checks were run. Full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/lifecycle.py` to add
  `_refresh_operator_hint_from_workspace()`.
- Changed `src/awf/runtime/pr_monitor_runner/mixins.py` to expose the lifecycle
  helper on the runner.
- Changed `src/awf/runtime/pr_monitor_runner/merge_loop.py` to re-run
  `decide()` after refreshing persisted operator-hint state before merge.
- Changed `tests/unit/runtime/test_pr_monitor_operator_hints.py` with
  `test_merge_rechecks_persisted_operator_hint_before_merge_pr`.

## Commands

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "merge_rechecks_persisted_operator_hint_before_merge_pr"`
  failed with `assert True is False`, proving the stale merge reached terminal
  merge handling.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "merge_rechecks_persisted_operator_hint_before_merge_pr"`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "operator_hint"`
  passed with `8 passed`.
- After implementation:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  passed.
