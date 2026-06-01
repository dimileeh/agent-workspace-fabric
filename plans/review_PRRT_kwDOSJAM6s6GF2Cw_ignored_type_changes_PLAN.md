# Review PRRT_kwDOSJAM6s6GF2Cw Ignored Type Changes Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GF2Cw` reports that validation cleanup can
miss type changes inside preserved ignored roots when an ignored snapshot entry
changes only by trailing slash form: a baseline empty directory becomes a file,
or a baseline file becomes an empty directory at the same normalized path.

Scope is limited to `src/awf/runtime/validation_worktree.py`, focused
regression tests in `tests/unit/runtime/test_validation_worktree.py`, and this
plan/validation documentation. Full AWF/GitHub validation remains owned by AWF
after the agent phase.

## Requirements Checklist

- Add regression coverage for a baseline empty ignored directory replaced by a
  file at the same normalized path.
- Add regression coverage for a baseline ignored file replaced by an empty
  directory at the same normalized path.
- Reject those type changes before cleanup can report success or preserve the
  replacement as setup-owned ignored state.
- Preserve existing ignored cleanup behavior for unchanged baseline ignored
  entries and generated ignored artifacts.
- Run only targeted tests and checks for the touched validation worktree files.
- Commit this thread fix locally without switching branches or pushing.

## Implementation Steps

1. Add the two focused failing tests in
   `tests/unit/runtime/test_validation_worktree.py`.
2. Run the new tests before implementation to confirm the reported gap.
3. Update ignored snapshot signature comparison to match baseline and current
   entries by normalized path while preserving the original path in messages.
4. Re-run the new regressions and adjacent validation worktree cleanup tests.
5. Run focused lint/type checks for the touched source and test files.
6. Save validation evidence in the matching validation document.
7. Stage only changed files and commit with a conventional message referencing
   the review thread.

## Verification Commands and Pass Criteria

- New regression tests fail before implementation and pass after implementation.
- Targeted adjacent validation worktree tests pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py`
  passes.
- Full AWF/GitHub validation is not run locally; AWF owns broad validation,
  provenance, logs, timeouts, and merge gating after completion.
