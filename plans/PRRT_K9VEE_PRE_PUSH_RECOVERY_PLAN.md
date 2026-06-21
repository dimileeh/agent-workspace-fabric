# PRRT_K9VEE Pre-Push Recovery Plan

## Problem Statement and Scope

The pre-push validation missing-HEAD recovery path can recover a local HEAD that contains protected-scope changes. If the protected-scope check rejects that recovered commit, the function currently returns a failure while leaving the worktree at the recovered commit.

Scope is limited to restoring the worktree to `recovery_head` before returning the protected-scope rejection result for thread `PRRT_kwDOSJAM6s6K9vee`.

## Requirements

- Add regression coverage for the recovered-HEAD protected-scope rejection path.
- Restore the worktree to `recovery_head` before returning the protected-scope failure.
- Preserve the existing reason code and failure message.
- Keep validation focused; broad AWF/GitHub validation is managed after agent completion.

## Implementation Steps

1. Update the existing recovered-HEAD protected-scope regression to assert cleanup is called with `restore_ref=recovery_head` and the returned workspace head is the recovery anchor.
2. Add cleanup handling in `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` immediately before the protected-scope failure return.
3. Run the targeted unit test covering this path.

## Verification

Command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k recovered_head_blocks_committed_protected_scope_violation
```

Pass criteria: the targeted test passes and confirms cleanup-to-`recovery_head` behavior.
