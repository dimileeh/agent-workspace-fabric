# PR #349 Validation Cleanup CI Fix Validation

Plan reference: `plans/CI_PR349_VALIDATION_CLEANUP_PLAN.md`

## Requirement Status

- Preserve validation side-effect cleanup before PR push and monitor retry
  validation: Complete.
- Restore executor-specific validation-worktree reason codes instead of masking
  them with `HEAD` capture infrastructure errors: Complete.
- Preserve mixed toolchain/real failure diagnostics and prefer the real non-127
  validation failure: Complete.
- Keep first-party files at or below 1500 lines without weakening the guard:
  Complete.
- Use focused local verification only, leaving broad AWF/GitHub validation to
  post-agent CI: Complete.
- Commit locally without switching branches or pushing: Complete.

## Evidence

- Updated `src/awf/control/executor/execution_validation.py` to capture the
  validation start `HEAD` before the clean-worktree precheck, while preserving
  dirty/status failure precedence.
- Added `src/awf/control/executor/validation_cleanup_guards.py` and moved
  cleanup guard/failure causality helpers out of the large validation loop.
- Updated mixed-127 PR-monitor fixtures to account for committed-fix cleanup
  head capture and retry validation head capture.
- Split oversized tests into:
  - `tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py`
  - `tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py`
  - `tests/unit/runtime/test_validation_worktree_head_cleanup.py`

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: `1 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_mixed_127_prefers_real_failure_for_fix_pass tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_tracked_validation_side_effect_cleans_before_pr_push tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_cleanup_failure_fails_validation_before_push tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_git_status_failure_preserves_status_error_message -q`
  - Result: `5 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_stale_callback_still_cleans_side_effects[True] tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_stale_callback_still_cleans_side_effects[False] tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_stale_callback_still_returns_stop_when_cleanup_fails[cancelled] tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_stale_callback_still_returns_stop_when_cleanup_fails[destroying] tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_cleanup_callback_terminal_after_stale_status_during_cleanup tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py::test_mixed_127_fix_commit_failure_reports_real_pre_push_details tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py::test_mixed_127_fix_pass_exhaustion_reports_real_pre_push_details -q`
  - Result: `7 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py -q`
  - Result: `6 passed`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_head_cleanup.py -q`
  - Result: `49 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py src/awf/control/executor/validation_cleanup_guards.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_head_cleanup.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/execution_validation.py src/awf/control/executor/validation_cleanup_guards.py`
  - Result: passed.
- `git diff --check`
  - Result: passed.

## Notes

Full coverage, whole-repository pytest, frontend builds, and CI-equivalent
validation were intentionally not run inside the agent phase per the AWF
workspace contract; AWF/GitHub owns those after completion.
