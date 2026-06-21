# Recovered Pre-Push Committed Diff Validation

Plan reference: `plans/RECOVERED_PRE_PUSH_COMMITTED_DIFF_PLAN.md`

## Requirement Status

- Complete: Recovered commits classify protected-file changes using the committed diff from `recovery_head..recovered`.
  - Evidence: `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` now calls `_protected_scope_violations_for_recovered_commit` with `base_ref=recovery_head` and the recovered changed paths.
- Complete: Recovered commits with protected-scope violations block before normal validation runs.
  - Evidence: `test_pre_push_validation_recovered_head_blocks_committed_protected_scope_violation` asserts the recovered committed-diff violation returns `PROTECTED_SCOPE_REPAIR_FAILED` and `validation.calls == []`.
- Complete: Diff/classification failures in recovered committed-diff validation produce the existing protected-scope-diff-unavailable failure.
  - Evidence: `test_pre_push_validation_recovered_head_committed_diff_error_blocks_validation` asserts `PROTECTED_SCOPE_DIFF_UNAVAILABLE` and no validation run.
- Complete: Existing agent-runtime ownership repair behavior remains before recovered protected-scope classification.
  - Evidence: `test_pre_push_validation_recovered_head_ownership_repair_failure_blocks_validation` asserts ownership failure blocks before committed-diff classification.
- Complete: Broad AWF/GitHub validation was not run.
  - Evidence: only focused commands below were executed; full AWF/GitHub validation is managed by AWF after agent completion.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k recovered_head`
  - Initial TDD run failed before implementation because `_protected_scope_violations_for_recovered_commit` was not available in `pre_push_validation`.
  - Final run passed: `5 passed, 5 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
  - Passed.

## Gaps

No planned requirements remain open. Full-suite validation, coverage gates, push, and PR updates are intentionally left to AWF/GitHub after agent completion.
