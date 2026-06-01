# PR #349 Validation Cleanup CI Fix Plan

## Problem Statement And Scope

PR #349 fails the `python-full-coverage` GitHub Actions job on targeted unit
tests around AWF validation worktree cleanup and on the first-party file
line-limit guard. Scope is limited to validation cleanup ordering, test fixture
updates that preserve the intended behavior, and mechanical file decomposition
needed to satisfy the 1500-line guard.

## Requirements Checklist

- [ ] Preserve validation side-effect cleanup before PR push and before monitor
      retry validation.
- [ ] Restore executor validation behavior so git-status failures and cleanup
      failures surface their specific validation-worktree reason codes instead
      of being masked by a `HEAD` capture infrastructure error.
- [ ] Preserve mixed toolchain/real failure diagnostics so real non-127
      validation failures remain preferred over missing-toolchain noise.
- [ ] Keep all first-party source and test files at or below 1500 lines without
      weakening the maintainability guard.
- [ ] Use only focused local verification; AWF/GitHub owns broad coverage and
      CI validation after this agent phase.
- [ ] Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Move executor validation `HEAD` capture before the clean-worktree precheck
   while still letting dirty/status precheck failures take precedence over a
   missing captured head.
2. Update the mixed-127 monitor fixtures to include the committed-fix cleanup
   `HEAD` capture and the subsequent retry validation `HEAD` where needed.
3. Decompose oversized validation modules/tests by moving cohesive helper or
   test groups into companion modules, keeping imports explicit and behavior
   unchanged.
4. Run the AWF-provided focused repro plus the remaining reported failing
   mixed/stale cleanup node IDs.
5. Run the focused line-limit guard and targeted Ruff checks for changed files.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_mixed_127_prefers_real_failure_for_fix_pass tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_tracked_validation_side_effect_cleans_before_pr_push tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_cleanup_failure_fails_validation_before_push tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_git_status_failure_preserves_status_error_message -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_stale_callback_still_cleans_side_effects[True] tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_stale_callback_still_cleans_side_effects[False] tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_stale_callback_still_returns_stop_when_cleanup_fails[cancelled] tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_stale_callback_still_returns_stop_when_cleanup_fails[destroying] tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup::test_executor_cleanup_callback_terminal_after_stale_status_during_cleanup tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py::test_mixed_127_fix_commit_failure_reports_real_pre_push_details tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py::test_mixed_127_fix_pass_exhaustion_reports_real_pre_push_details -q
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev ruff check <changed python files>
```

Pass criteria: all focused commands pass. Full coverage, whole-repository
pytest, frontend builds, and CI-equivalent validation remain managed by
AWF/GitHub after agent completion.
