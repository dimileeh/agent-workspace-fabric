# PRRT_kwDOSJAM6s6Ky7ri Plan

## Problem Statement and Scope

The sync-base conflict repair path in `remote_ops._run_sync_base` has compose
context available, but its call to `_commit_dirty_worktree` does not pass
`compose_project` or `compose_file`. The commit sink only runs protected-scope
repair when that context is present, so recovered sync-base commits can bypass
the intended protected-scope gate.

Scope is limited to threading the existing compose context through the
sync-base conflict commit path and adding a focused regression test.

## Requirements Checklist

- Verify the review claim against current code before editing.
- Preserve existing sync-base behavior except for forwarding compose context.
- Add a focused regression test that fails without the forwarding fix.
- Run only targeted validation for the changed behavior; AWF/GitHub own broad
  validation after agent completion.
- Commit the fix locally without pushing or switching branches.

## Implementation Steps

1. Add a regression test for the sync-base conflict path that captures
   `_commit_dirty_worktree` arguments and asserts compose context is forwarded.
2. Update `_run_sync_base` to pass `compose_project` and `compose_file` into
   `_commit_dirty_worktree`.
3. Run the focused pytest for the regression file.
4. Create the validation document with evidence and commit the scoped changes.

## Verification

Command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q
```

Pass criteria: the focused sync-base regression file passes.
