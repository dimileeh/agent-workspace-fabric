# PRRT_K37OF Sync-Base Git Env Plan

## Problem Statement And Scope

The PR review thread `PRRT_kwDOSJAM6s6K37OF` reports that sync-base worktree git
commands inherit `GIT_OBJECT_DIRECTORY` and `GIT_ALTERNATE_OBJECT_DIRECTORIES`
from the monitor process environment. Other hardened monitor git paths strip
these variables before running git so private object stores cannot influence
refs or trees that the canonical mirror cannot later resolve.

Scope is limited to the sync-base helper in
`src/awf/runtime/pr_monitor_runner/remote_ops.py` and focused regression tests
for that behavior.

## Requirements Checklist

- Confirm whether `_run_sync_base` worktree git commands currently lack the
  object lookup environment sanitizer.
- Make sync-base worktree git runner calls pass an environment with git object
  lookup override variables removed.
- Add or update focused regression coverage showing sync-base git calls receive
  the sanitized environment.
- Keep validation narrow and record that full AWF/GitHub validation is owned by
  AWF after agent completion.

## Implementation Steps

1. Inspect `_run_sync_base` and nearby hardened git paths.
2. Update `_run_sync_base`'s local `_git` helper to pass
   `git_env_without_object_lookup_overrides()` to the shared command runner.
3. Update focused sync-base tests and add a regression assertion for sanitized
   env handling.
4. Run targeted unit tests for the edited sync-base test module.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
  - Passes with no failures.

Full AWF/GitHub validation, coverage gates, and broad suites are intentionally
not run in the agent phase per the AWF workspace contract.
