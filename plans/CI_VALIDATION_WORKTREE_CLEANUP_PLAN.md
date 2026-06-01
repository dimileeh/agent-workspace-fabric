# CI Validation Worktree Cleanup Plan

## Problem Statement

PR #349 is failing focused Python CI checks around validation worktree cleanup and
pre-push validation rollback cleanup. The reported failures show two issues:

- validation cleanup tests expect older `git restore` argument shapes while the
  implementation uses literal pathspec mode;
- pre-push validation rollback no longer records the expected `git clean -fdx`
  cleanup behavior in the focused rollback tests;
- `tests/unit/runtime/test_validation_worktree.py` exceeds the first-party
  file line limit.

## Scope

Keep the fix limited to validation worktree cleanup behavior/tests and test file
decomposition. Do not change CI gates, branch management, GitHub workflows, or
AWF validation policy.

## Requirements Checklist

- Reproduce the AWF-provided focused CI failures before changing code.
- Preserve literal-pathspec safety for tracked restore and untracked cleanup.
- Make the pre-push rollback tests assert the cleanup command shape actually
  used by validation worktree cleanup.
- Split validation worktree tests so every first-party code file is under the
  1500-line maintainability limit.
- Run focused tests only; leave broad AWF/GitHub validation to AWF after agent
  completion.
- Commit the local fix without switching branches or pushing.

## Implementation Steps

1. Update head-cleanup test command expectations to include literal pathspec
   restore arguments.
2. Update pre-push rollback cleanup assertions to recognize literal-pathspec
   `git clean -ffdx` while still proving ignored paths are preserved.
3. Move a coherent set of validation worktree tests into a new focused test
   module, reusing existing helper constants from `test_validation_worktree.py`.
4. Run the AWF-provided focused repro command and the line-limit test.
5. Record focused verification evidence in a validation document.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py::test_pre_push_validation_fix_pass_rolls_back_when_commit_fails tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py::test_pre_push_validation_fix_pass_rolls_back_when_commit_raises tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py::test_pre_push_validation_fix_pass_rollback_preserves_ignored_paths tests/unit/runtime/test_validation_worktree_head_cleanup.py::test_cleanup_validation_worktree_rolls_back_head_when_verify_status_fails tests/unit/runtime/test_validation_worktree_head_cleanup.py::test_cleanup_validation_worktree_marks_restored_tracked_changes_as_clean_after_cleanup -q
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Pass criteria: all focused tests pass locally. Full AWF/GitHub validation is
managed by AWF after agent completion.
