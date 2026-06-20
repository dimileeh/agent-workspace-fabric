# Sync-Base Post-Agent Mirror Hooks Plan

## Problem Statement and Scope

An unresolved review thread reports that the sync-base conflict-agent plumbing
exception path logs a failed `repair_mirror_hooks_path()` call but re-raises the
original adapter/plumbing exception. If the mirror remains poisoned, sibling
workspaces can inherit the bad `core.hooksPath`. Scope is limited to the
post-agent sync-base exception path in `remote_ops.py` and a focused regression
test.

## Requirements Checklist

- Verify the reported path against current code before changing behavior.
- Add a regression test proving a failed post-agent mirror-hooks repair raises
  the mirror-hooks failure instead of the original cleanup/plumbing exception.
- Change only the sync-base post-agent mirror-hooks repair failure behavior.
- Run targeted tests for the changed behavior only.
- Record validation evidence and note that broad AWF/GitHub validation is
  managed after agent completion.

## Implementation Steps

1. Inspect `src/awf/runtime/pr_monitor_runner/remote_ops.py` around the
   sync-base conflict-agent exception handler.
2. Add a focused unit test beside existing sync-base mirror hook repair tests.
3. Update the exception handler to raise
   `_MonitorMirrorHooksPathRepairFailedError` when post-agent repair fails.
4. Run the targeted test file or selected tests covering the new behavior.
5. Write `plans/SYNC_BASE_POST_AGENT_MIRROR_HOOKS_VALIDATION.md`.
