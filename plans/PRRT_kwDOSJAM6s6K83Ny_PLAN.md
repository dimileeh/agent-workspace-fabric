# PRRT_kwDOSJAM6s6K83Ny Plan

## Problem Statement

The review thread reports that `_repair_operation_start_head_result` accepts a
successful `git rev-parse HEAD` result without object validation when
`mirror_path_for_worktree()` returns `None`. Scope is limited to the primary
repair-start HEAD capture path in `remote_repair.py`.

## Requirements

- Add a focused regression test proving a no-mirror primary `rev-parse HEAD`
  SHA is rejected when the worktree object database cannot resolve it.
- Validate the captured primary SHA with `cat-file -e <sha>^{commit}` even
  when no mirror path is discoverable.
- Preserve existing mirror validation and fallback behavior.
- Run only focused validation for the touched behavior; full AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a unit test covering the no-mirror primary HEAD poisoning case.
2. Update `_repair_operation_start_head_result` to use the worktree object
   check when the mirror path is unavailable.
3. Adjust any focused expectations made stale by the added primary object check.
4. Run the new regression and the focused repair-start test selection.

## Verification

- New regression fails before the implementation change.
- Focused repair-start tests pass after implementation.
- Focused lint passes for changed Python files.
