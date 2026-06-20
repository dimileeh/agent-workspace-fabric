# PRRT_kwDOSJAM6s6K4aW- Plan

## Problem Statement and Scope

The CI repair path repairs agent runtime ownership before launching its fix
agent, but it does not repair a poisoned shared mirror `core.hooksPath` until
the later dirty-worktree commit sink. If the CI fix agent self-commits, that
commit can bypass installed hooks.

Scope is limited to the CI repair launch path in
`src/awf/runtime/pr_monitor_runner/ci_ops.py` and focused regression coverage.

## Requirements Checklist

- Repair mirror `core.hooksPath` before launching the CI fix adapter.
- Fail closed without launching the adapter when mirror hook repair fails.
- Preserve existing post-agent `_commit_dirty_worktree()` behavior.
- Add focused regression tests for ordering and failure behavior.
- Run only targeted validation; full AWF/GitHub validation remains AWF-managed.

## Implementation Steps

1. Add regression tests around `_run_ci_fix()` proving mirror hook repair runs
   before the adapter and a repair failure stops before adapter launch.
2. Import the existing mirror hook repair helpers into `ci_ops.py`.
3. Add the same mirror-path detection and fail-closed repair guard used by
   adjacent monitor agent paths after runtime ownership repair and before
   `adapter.run()`.
4. Return a reason-coded `_GitPushResult` on repair failure.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py -q -k "run_ci_fix"`
  - Passes, including the new focused regression tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_task_tag_threading.py`
  - Passes.

Full repository validation, coverage, and PR merge gates are intentionally not
run in-agent because AWF owns that validation after agent completion.
