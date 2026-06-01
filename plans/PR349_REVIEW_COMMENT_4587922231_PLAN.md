# PR349 Review Comment 4587922231 Plan

## Problem Statement and Scope

Address the review-level comment for PR #349 about structural issues in
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py`.

The quoted evidence reports:

- a duplicate block of pre-push helper functions,
- an unreachable fallback return after `_run_pre_push_validation_with_fix_passes`
  enters its `while True` retry loop,
- a prior executor validation double-finish concern.

Current-source inspection is part of the scope because review-level comments can
be stale by the time the agent workspace is created.

## Requirements Checklist

- Confirm whether the helper-function duplicate exists in the current branch.
- Remove any valid unreachable fallback code from the pre-push validation retry
  loop without changing runtime behavior.
- Confirm whether the executor validation double-finish concern still exists in
  the current branch.
- Add focused regression coverage for the valid structural issue.
- Run only focused local checks; full AWF/GitHub validation remains owned by AWF
  after agent completion.
- Commit the scoped fix locally and print the AWF verdict.

## Implementation Steps

1. Inspect the current source for duplicated helper definitions, dead fallback
   code, and the executor worktree guard call.
2. Add an AST-based regression test that fails while the unreachable fallback is
   present and also locks in one definition of the named helper functions.
3. Run the new focused test and record the expected failure.
4. Remove the unreachable fallback return after the `while True` loop.
5. Run the focused test and a narrow lint check for the changed Python files.
6. Write validation notes in
   `plans/PR349_REVIEW_COMMENT_4587922231_VALIDATION.md`.
7. Stage only changed files, commit locally, and print the verdict.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q -k structural`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`

Pass criteria:

- The structural regression test fails before the implementation change and
  passes after it.
- The focused ruff check passes for changed Python files.
- No broad validation, full coverage, full frontend build, push, rebase, or
  branch switch is performed.
