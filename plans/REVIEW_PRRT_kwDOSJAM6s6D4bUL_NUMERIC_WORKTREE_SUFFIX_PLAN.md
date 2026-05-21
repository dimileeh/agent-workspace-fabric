# PRRT_kwDOSJAM6s6D4bUL Numeric Worktree Suffix Plan

## Problem Statement And Scope

Runtime ownership repair currently requires the linked-worktree metadata
directory name to exactly match `workspace_id`. Git may create a valid linked
worktree admin directory with a numeric suffix when the preferred basename is
already taken, so exact name matching can reject legitimate AWF worktrees.

Scope is limited to `src/awf/runtime/ownership.py` and focused runtime ownership
regressions for PR review thread `PRRT_kwDOSJAM6s6D4bUL`.

## Requirements Checklist

- Preserve the existing safety behavior that rejects unverified numeric suffixes
  and workspace-id prefix collisions.
- Add a failing regression proving a Git-style numeric-suffixed metadata
  directory is accepted when it has a reciprocal `gitdir` back-reference to the
  current worktree's `.git` file.
- Add or preserve regression coverage proving a numeric-suffixed metadata
  directory is rejected when its `gitdir` back-reference points at another
  worktree.
- Update runtime ownership validation to allow only exact metadata names or
  numeric-suffixed names that point back to the current worktree.
- Run focused ownership tests and lint for changed Python files.

## Implementation Steps

1. Add the valid numeric-suffix regression and the cross-worktree back-reference
   rejection regression beside existing ownership repair safety tests.
2. Run the valid numeric-suffix regression to confirm it fails before the
   implementation.
3. Add a small metadata back-reference validator and use it only for
   numeric-suffixed linked worktree admin directories.
4. Re-run the focused ownership test file and lint for the changed Python files.
5. Record validation results in the matching validation document.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py::test_repair_agent_runtime_ownership_allows_verified_numeric_worktree_suffix -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/ownership.py tests/unit/runtime/test_ownership.py`

Pass criteria: the new valid suffix test fails before implementation, the full
focused ownership test file passes after implementation, and lint reports no
issues for the changed Python files.
