# Validation Worktree Empty Parents Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6GHluf` reports that validation cleanup removes
untracked generated files but can leave their newly-created empty parent
directories behind. The post-cleanup worktree verification snapshots empty
untracked directories, so a cleanable validation side effect can become
`VALIDATION_WORKTREE_CLEANUP_FAILED`.

## Scope

- Fix `src/awf/runtime/validation_worktree.py` so cleanup removes empty
  non-ignored parent directories left after untracked generated files are
  cleaned.
- Preserve existing ignored-root cleanup behavior and its protections for
  baseline ignored directories.
- Add a focused regression test in `tests/unit/runtime/test_validation_worktree.py`.
- Do not run broad AWF/GitHub-owned validation; AWF owns full validation after
  agent completion.

## Requirements Checklist

- [ ] Add a failing regression for an untracked generated file under a new
  non-ignored directory where `git clean` removes only the file path.
- [ ] Remove empty non-ignored parent directories after successful untracked
  cleanup.
- [ ] Keep cleanup scoped to generated path parents; do not remove the worktree
  root or ignored-root baseline directories.
- [ ] Report cleanup failure with existing validation cleanup failure semantics
  if an empty parent cannot be removed.
- [ ] Run targeted tests for validation worktree cleanup behavior only.

## Implementation Steps

1. Add a regression test that simulates `?? gen/out.txt`, makes `git clean`
   remove `gen/out.txt`, and expects cleanup to remove the now-empty `gen/`
   directory before final verification.
2. Confirm the regression fails before the implementation change.
3. Add a small helper to compute and remove empty non-ignored cleanup parent
   directories, ordered deepest-first.
4. Invoke the helper after successful `git clean` and before verification.
5. Run the focused regression and the narrow validation worktree test module.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`

Pass criteria: the focused validation worktree tests pass. Full AWF/GitHub
validation remains managed by AWF after this agent exits.
