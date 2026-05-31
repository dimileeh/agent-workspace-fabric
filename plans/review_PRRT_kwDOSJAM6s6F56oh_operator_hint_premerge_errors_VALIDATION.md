# Review PRRT_kwDOSJAM6s6F56oh Operator Hint Pre-Merge Errors Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6F56oh_operator_hint_premerge_errors_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving a persisted pending operator hint
  refreshed during merge handling is dispatched even when pre-merge recheck
  raises `BaseFetchError`.
- Complete: Preserved existing pre-merge recheck error behavior when no
  refreshed operator hint exists.
- Complete: Kept the implementation scoped to merge-loop ordering.
- Complete: Ran focused tests and lint only. Full AWF/GitHub validation remains
  owned by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`
- `plans/review_PRRT_kwDOSJAM6s6F56oh_operator_hint_premerge_errors_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6F56oh_operator_hint_premerge_errors_VALIDATION.md`

Focused verification:

- Before the implementation fix, the new regression failed with `assert True is
  False`, showing the workspace terminated instead of dispatching the
  operator-hint repair action.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_merge_recheck_dispatches_persisted_operator_hint_before_pre_merge_error -q`
  passed after the fix and again after the final test assertion cleanup.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py::test_pre_merge_recheck_github_error_fails_workspace tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py::test_pre_merge_recheck_base_fetch_error_fails_workspace tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py::test_pre_merge_recheck_base_behind_error_fails_workspace -q`
  passed with `14 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  passed.

## Gaps

None.
