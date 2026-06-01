# Review PRRT_kwDOSJAM6s6GDo7r Cleanup Head Rollback Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6GDo7r_cleanup_head_rollback_PLAN.md`

## Requirement Status

- Add regression coverage proving failed `git restore` rolls back HEAD when
  validation moved HEAD: Complete.
- Add regression coverage proving failed `git clean` rolls back HEAD when
  validation moved HEAD: Complete.
- Preserve existing cleanup failure behavior when HEAD did not move or no
  `restore_ref` was captured: Complete for the thread scope; the adjacent
  tracked-restore stderr regression still reports `git restore` when HEAD is
  unchanged.
- Keep validation focused; full AWF/GitHub validation remains owned by AWF after
  agent completion: Complete.
- Commit this thread fix locally without switching branches or pushing:
  Complete; this validation artifact is included in the local thread-fix commit.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/review_PRRT_kwDOSJAM6s6GDo7r_cleanup_head_rollback_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6GDo7r_cleanup_head_rollback_VALIDATION.md`

Focused checks:

- Pre-implementation regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q -k 'rolls_back_head_when_restore_fails or rolls_back_head_when_clean_fails'`
  failed with both new tests reporting `git restore`/`git clean` instead of
  `git reset --hard`.
- Post-implementation regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q -k 'rolls_back_head_when_restore_fails or rolls_back_head_when_clean_fails'`
  passed: 2 passed, 29 deselected.
- Adjacent tracked-restore regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q -k 'restores_tracked_files_with_none_stderr or rolls_back_head_when_restore_fails or rolls_back_head_when_clean_fails'`
  passed: 3 passed, 28 deselected.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  passed.

Additional observation:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  was attempted and failed in four existing restore-ref-missing untracked
  cleanup tests. Those expectations conflict with the existing
  `test_cleanup_validation_worktree_fails_for_untracked_dirty_state_when_restore_ref_missing`
  policy test and are outside this review thread's HEAD rollback scope.

Full AWF/GitHub validation was not run locally because AWF owns broad validation
after agent completion.

## Gaps

No remaining gaps for review thread `PRRT_kwDOSJAM6s6GDo7r`.
