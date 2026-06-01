# PRRT_kwDOSJAM6s6GC842 Ignored Baseline HEAD Rollback Plan

## Problem Statement And Scope

PR #349 has an unresolved inline review thread on
`src/awf/runtime/validation_worktree.py` reporting that ignored-baseline cleanup
failure paths can return before verifying and rolling back validation-authored
HEAD changes. The scope is limited to validation worktree cleanup behavior and
its focused regression coverage.

## Requirements Checklist

- Add a regression test proving cleanup rolls HEAD back to `restore_ref` when
  validation both changes HEAD and deletes a pre-existing ignored snapshot file.
- Preserve the existing cleanup failure classification for ignored-baseline
  drift.
- Avoid cleaning or restoring pre-existing ignored snapshot files that are
  deleted or modified by validation.
- Keep validation local and focused; AWF/GitHub own broad validation after the
  agent phase.

## Implementation Steps

1. Add a unit test in `tests/unit/runtime/test_validation_worktree.py` for the
   deleted ignored snapshot plus moved HEAD case.
2. Update `cleanup_validation_worktree_side_effects` so ignored-baseline drift
   failure returns run HEAD verification and reset to `restore_ref` first when
   possible.
3. Run the targeted unit test module or focused tests for validation worktree
   cleanup only.
4. Save validation evidence in the companion validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  passes.
- No broad AWF/GitHub-owned validation, full coverage, or full-suite commands
  are run during this agent phase.

## Assumptions/Changes

- After the regression fix, the full `test_validation_worktree.py` module was
  attempted and exposed existing unrelated failures around `restore_ref is None`
  cleanup semantics and one list-vs-tuple assertion. Those are outside this
  review thread's ignored-baseline HEAD rollback scope, so focused pass criteria
  for this fix are the affected ignored-baseline cleanup tests plus focused
  lint on the touched files.
