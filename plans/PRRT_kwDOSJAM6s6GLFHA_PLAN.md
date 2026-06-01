# PRRT_kwDOSJAM6s6GLFHA Plan

## Problem Statement and Scope

PR #349 has an unresolved inline review thread on
`src/awf/runtime/validation_worktree.py` reporting that validation worktree
cleanup uses `git clean` with a single `-f`. Git can leave nested repositories
behind unless clean is forced twice. The scope is limited to validation-created
untracked or ignored cleanup paths and the regression coverage for that behavior.

## Requirements Checklist

- Add a focused regression test showing validation cleanup removes a nested Git
  repository created below a preserved ignored root.
- Update validation worktree cleanup to force-clean nested repositories without
  weakening existing safety checks for pre-existing ignored roots or tracked
  changes.
- Run only targeted validation for the touched behavior.
- Document validation evidence in
  `plans/PRRT_kwDOSJAM6s6GLFHA_VALIDATION.md`.

## Implementation Steps

1. Inspect existing validation worktree tests and reuse their helper patterns.
2. Add the failing nested-repository regression test.
3. Change the cleanup command from a single-force clean to a double-force clean
   for selected cleanup pathspecs.
4. Run the targeted test before and after the code change when practical, then
   run the focused test file or relevant focused selection.
5. Commit only the changed files with a conventional commit message referencing
   the review thread id.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::<new test> -q`
  - The new regression test fails before the cleanup command change and passes
    after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  - The focused validation worktree unit tests pass.
- Full AWF/GitHub validation is intentionally not run during the agent phase;
  AWF owns broad validation and merge gating after completion.
