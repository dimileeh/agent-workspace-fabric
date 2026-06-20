# PRRT_kwDOSJAM6s6K4Akc Plan

## Problem Statement and Scope

The PR monitor comment-repair agent path repairs agent runtime ownership before
launching the adapter, but it does not repair a poisoned shared mirror
`core.hooksPath` until `_commit_dirty_worktree()` runs after the adapter. If the
agent self-commits during its run, that commit can bypass the mirror hooks.

Scope is limited to the comment-repair CLI launch path in
`src/awf/runtime/pr_monitor_runner/comments.py` and focused regression coverage.

## Requirements Checklist

- Repair mirror `core.hooksPath` before launching the comment-repair adapter.
- Fail closed without launching the adapter when mirror hook repair fails.
- Preserve the existing post-agent `_commit_dirty_worktree()` behavior.
- Add focused regression tests for ordering and failure behavior.
- Run only targeted validation; full AWF/GitHub validation remains AWF-managed.

## Implementation Steps

1. Add the same mirror-path detection and `repair_mirror_hooks_path()` guard used
   by pre-push fix-pass paths before `adapter.run()` in `_invoke_cli_for_verdict_result()`.
2. Log failure evidence with the standard `MIRROR_HOOKS_PATH_POISONED` reason.
3. Raise `_MonitorMirrorHooksPathRepairFailedError` on repair failure so existing
   monitor callers handle it as a terminal fail-closed monitor policy condition.
4. Add unit tests that prove repair precedes adapter launch and failed repair
   prevents adapter launch.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py -q`
  - Passes, including the new focused regression tests.

Full repository validation, coverage, and PR merge gates are intentionally not
run in-agent because AWF owns that validation after agent completion.
