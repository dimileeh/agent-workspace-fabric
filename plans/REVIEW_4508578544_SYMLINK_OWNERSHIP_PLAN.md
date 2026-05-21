# Review 4508578544 Symlink Ownership Plan

## Problem Statement and Scope

Address PR review comment `issue:4508578544` about runtime ownership repair rejecting
valid workspaces when the AWF mirror path is reached through a symlinked prefix.
The scope is limited to the ownership layout validation helper and a focused
regression test.

## Requirements Checklist

- Add a regression test showing a valid linked worktree whose `.git` metadata
  points through a symlinked mirror prefix is accepted.
- Preserve existing safety checks that reject mirrors outside the expected AWF
  mirror root or metadata for another workspace.
- Fix the parent comparison so equivalent resolved paths compare equal.
- Keep the change narrow and commit it locally without pushing.

## Implementation Steps

1. Add the failing symlink-prefix regression test in
   `tests/unit/runtime/test_ownership.py`.
2. Run the targeted test to confirm it fails before the code change.
3. Update `src/awf/runtime/ownership.py` to resolve the linked-worktree metadata
   parent before comparing it with the resolved expected parent.
4. Re-run the focused ownership tests.
5. Create the validation document and commit the changed files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py -q`
  passes after the fix.
- The targeted symlink regression test fails before the implementation change.
