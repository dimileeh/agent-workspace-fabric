# PRRT_kwDOSJAM6s6GEIV7 Status Failure HEAD Rollback Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GEIV7_STATUS_FAILURE_HEAD_ROLLBACK_PLAN.md`

## Requirement Status

- Complete: Add regression coverage proving an initial status failure still
  verifies and rolls back HEAD when `restore_ref` is available.
- Complete: Add regression coverage proving a post-cleanup verify status
  failure still verifies and rolls back HEAD when `restore_ref` is available.
- Complete: Preserve existing status-failure reason-code behavior when HEAD is
  unchanged.
- Complete: Reuse the existing `_verify_head_unchanged` rollback path instead
  of adding a second rollback implementation.
- Complete: Run only focused validation for the changed behavior; full
  AWF/GitHub validation remains owned by AWF after agent completion.

## Evidence

Changed files:

- `plans/PRRT_kwDOSJAM6s6GEIV7_STATUS_FAILURE_HEAD_ROLLBACK_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GEIV7_STATUS_FAILURE_HEAD_ROLLBACK_VALIDATION.md`
- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`

Focused red check before implementation:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_rolls_back_head_when_initial_status_fails \
  tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_rolls_back_head_when_verify_status_fails \
  -q
```

Result: failed as expected with both tests returning
`VALIDATION_WORKTREE_STATUS_FAILED` before rollback.

Focused checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_rolls_back_head_when_initial_status_fails \
  tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_verify_status_failure_is_preserved \
  tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_rolls_back_head_when_verify_status_fails \
  -q
```

Result: passed, `3 passed in 0.60s`.

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/runtime/validation_worktree.py \
  tests/unit/runtime/test_validation_worktree.py
```

Result: passed.

Additional attempted focused-file run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q
```

Result: failed in tests outside the status-failure rollback change area:

- `test_cleanup_validation_worktree_cleans_untracked_files_with_none_stderr`
- `test_cleanup_validation_worktree_ignores_pre_existing_ignored_paths_in_cleanup`
- `test_cleanup_validation_worktree_fails_ignored_snapshot_when_no_stderr`
- `test_cleanup_validation_worktree_marks_untracked_files_as_clean_after_cleanup`

Those failures exercise restore-ref-missing untracked/ignored cleanup behavior
and are not part of PRRT_kwDOSJAM6s6GEIV7. Full AWF/GitHub validation is left
to AWF after agent completion per the workspace contract.

## Gaps

No gaps remain for the planned review-thread requirements.
