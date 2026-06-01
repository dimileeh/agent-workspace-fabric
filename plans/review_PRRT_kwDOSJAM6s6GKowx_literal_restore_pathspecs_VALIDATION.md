# PRRT_kwDOSJAM6s6GKowx Literal Restore Pathspecs Validation

Plan reference:
`review_PRRT_kwDOSJAM6s6GKowx_literal_restore_pathspecs_PLAN.md`

## Requirement Status

- Complete: Add a regression test showing tracked validation dirt with a
  pathspec-magic filename is restored literally.
- Complete: Use literal pathspec handling for tracked restore cleanup, matching
  the existing untracked `git clean` cleanup path.
- Complete: Preserve existing cleanup behavior for ordinary tracked and
  untracked paths.
- Complete: Commit only the files changed for this review thread.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/review_PRRT_kwDOSJAM6s6GKowx_literal_restore_pathspecs_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6GKowx_literal_restore_pathspecs_VALIDATION.md`

Commands run:

- Red check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q -k tracked_pathspec_magic_path_literally`
  failed with `VALIDATION_WORKTREE_CLEANUP_FAILED` because plain
  `git restore` rejected `:(glob)foo` as pathspec syntax.
- Focused green check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q -k "tracked_pathspec_magic_path_literally or restores_tracked_path_under_ignored_root or cleans_generated_ignored_metachar_path_literally"`
  passed: 3 passed, 33 deselected.
- Focused runtime helper check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  passed: 36 passed.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  passed.

Full AWF/GitHub validation was not executed locally, per the workspace contract;
AWF/GitHub own broad validation, provenance, logs, and merge gating after agent
completion.
