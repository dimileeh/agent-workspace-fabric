# Comment 3335511834 Literal Ignored Paths Validation

Plan reference: `COMMENT_3335511834_LITERAL_IGNORED_PATHS_PLAN.md`

## Requirement Status

- Use literal pathspec handling for ignored snapshot `git ls-files` commands:
  Complete. `_snapshot_ignored_paths` now invokes `git ls-files` through
  `--literal-pathspecs`.
- Add a regression test covering a literal ignored root named `:(glob)cache/`:
  Complete. The new real-Git unit test verifies the baseline snapshot includes
  `:(glob)cache/baseline.txt`.
- Preserve existing cleanup behavior for tracked restore and generated ignored
  artifact removal: Complete. Existing validation worktree unit tests pass with
  the updated snapshot command shape.
- Run focused validation only: Complete. No broad AWF/GitHub validation or
  coverage gate was run locally.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/COMMENT_3335511834_LITERAL_IGNORED_PATHS_PLAN.md`
- `plans/COMMENT_3335511834_LITERAL_IGNORED_PATHS_VALIDATION.md`

Commands run:

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_snapshots_pathspec_magic_ignored_root_literally -q`
  failed because the ignored snapshot was empty.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_snapshots_pathspec_magic_ignored_root_literally -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  passed: 38 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion per the
workspace contract.
