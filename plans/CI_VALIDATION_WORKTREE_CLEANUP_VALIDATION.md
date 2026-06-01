# CI Validation Worktree Cleanup Validation

Plan reference: `CI_VALIDATION_WORKTREE_CLEANUP_PLAN.md`

## Requirement Status

- Reproduce the AWF-provided focused CI failures before changing code:
  Complete. The provided five-test repro failed locally before edits with the
  same cleanup command-shape assertions reported by CI.
- Preserve literal-pathspec safety for tracked restore and untracked cleanup:
  Complete. Tests now expect the shared literal-pathspec restore prefix and the
  existing `git clean -ffdx` cleanup shape.
- Make the pre-push rollback tests assert the cleanup command shape actually
  used by validation worktree cleanup:
  Complete. Rollback assertions now check `clean -ffdx`, including ignored-path
  preservation.
- Split validation worktree tests so every first-party code file is under the
  1500-line maintainability limit:
  Complete. Head-verification cleanup cases were moved from
  `test_validation_worktree.py` to `test_validation_worktree_head_cleanup.py`;
  the source file is now 1471 lines.
- Run focused tests only; leave broad AWF/GitHub validation to AWF after agent
  completion:
  Complete. Only targeted pytest node IDs and a focused Ruff check were run.
- Commit the local fix without switching branches or pushing:
  Complete. The final local commit for this fix includes the focused test
  updates and required plan/validation artifacts.

## Evidence

Files changed:

- `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree_head_cleanup.py`
- `plans/CI_VALIDATION_WORKTREE_CLEANUP_PLAN.md`
- `plans/CI_VALIDATION_WORKTREE_CLEANUP_VALIDATION.md`

Focused commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py::test_pre_push_validation_fix_pass_rolls_back_when_commit_fails tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py::test_pre_push_validation_fix_pass_rolls_back_when_commit_raises tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py::test_pre_push_validation_fix_pass_rollback_preserves_ignored_paths tests/unit/runtime/test_validation_worktree_head_cleanup.py::test_cleanup_validation_worktree_rolls_back_head_when_verify_status_fails tests/unit/runtime/test_validation_worktree_head_cleanup.py::test_cleanup_validation_worktree_marks_restored_tracked_changes_as_clean_after_cleanup -q
```

Result after implementation: `5 passed in 2.62s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Result after implementation: `1 passed in 0.88s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree_head_cleanup.py::test_cleanup_validation_worktree_rolls_back_head_when_deleted_ignored_snapshot_fails tests/unit/runtime/test_validation_worktree_head_cleanup.py::test_cleanup_validation_worktree_verify_check_does_not_report_status_as_cleanup_command tests/unit/runtime/test_validation_worktree_head_cleanup.py::test_cleanup_validation_worktree_rollback_to_restore_ref_when_restored_tracked_state_is_dirty tests/unit/runtime/test_validation_worktree_head_cleanup.py::test_cleanup_validation_worktree_verify_status_failure_is_preserved -q
```

Result after implementation: `4 passed in 0.54s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_head_cleanup.py
```

Result after implementation: `All checks passed!`.

Full AWF/GitHub validation was not run locally; AWF owns broad validation,
provenance, logs, timeouts, and merge gating after agent completion.
