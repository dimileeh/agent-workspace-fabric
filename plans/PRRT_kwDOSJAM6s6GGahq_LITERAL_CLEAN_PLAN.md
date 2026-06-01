# PRRT_kwDOSJAM6s6GGahq Literal Clean Plan

## Problem Statement and Scope

The review thread reports that validation-worktree cleanup passes generated
untracked or ignored paths to `git clean` as ordinary pathspecs. Paths containing
Git pathspec metacharacters, such as `[` and `]`, can match and remove preserved
ignored baseline files. Scope is limited to validation cleanup's generated-path
`git clean` invocation and focused regression coverage.

## Requirements Checklist

- Add a regression proving a generated ignored path with pathspec
  metacharacters is cleaned without removing a preserved ignored baseline file.
- Make validation cleanup invoke `git clean` with literal pathspec semantics.
- Preserve existing cleanup failure behavior, reason codes, and HEAD rollback
  checks.
- Run focused validation only; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Add a focused unit regression in `tests/unit/runtime/test_validation_worktree.py`
   using a real temporary Git worktree with `.venv/foo1` preserved and
   `.venv/foo[1]` generated.
2. Run that single test first and confirm it fails against the current
   non-literal `git clean` invocation.
3. Update `src/awf/runtime/validation_worktree.py` so generated untracked paths
   are cleaned through `git --literal-pathspecs clean -fdx -- ...`.
4. Update command-expectation tests that assert the exact `git clean` argument
   vector.
5. Re-run the new regression and nearby validation-worktree cleanup tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_cleans_generated_ignored_metachar_path_literally -q`
  - First run should fail before implementation.
  - Final run should pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q -k 'clean or ignored'`
  - Focused cleanup/ignored-path surface should pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  - Touched Python files should pass lint.

## Assumptions/Changes

- The broader `-k 'clean or ignored'` selection includes existing tests whose
  expectations conflict with the current restore-ref guard and one pre-existing
  list-vs-tuple command assertion. Those failures are outside this review
  thread's literal-pathspec scope, so final verification uses the narrower
  cleanup tests directly exercising the changed `git clean` path. AWF/GitHub
  owns broad validation after agent completion.
