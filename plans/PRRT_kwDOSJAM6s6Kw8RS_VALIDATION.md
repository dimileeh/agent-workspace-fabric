# PRRT_kwDOSJAM6s6Kw8RS Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Kw8RS_PLAN.md`

## Requirement Status

- Complete: Added a regression proving `check-ignore` failure during cleanup
  leaves previously discovered empty directories in place.
- Complete: `_remove_empty_untracked_dirs` now completes ignore probing before
  any `rmdir` mutation.
- Complete: Returned cleanup paths remain limited to directories actually
  removed.
- Complete: The code change is minimal and localized.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/PRRT_kwDOSJAM6s6Kw8RS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Kw8RS_VALIDATION.md`

Focused checks:

- Failed before fix as expected: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_remove_empty_untracked_dirs_does_not_partially_clean_when_check_ignore_fails -q`
- Passed after fix: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_remove_empty_untracked_dirs_does_not_partially_clean_when_check_ignore_fails -q`
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -k remove_empty_untracked_dirs -q`
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree_wildcard_ignored.py -k remove_empty_untracked_dirs -q`
- Passed: `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`

Full AWF/GitHub validation was not run inside the agent phase per workspace
contract; AWF owns broad validation after completion.
