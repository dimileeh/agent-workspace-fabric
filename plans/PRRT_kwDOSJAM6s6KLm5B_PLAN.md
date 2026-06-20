# PRRT_kwDOSJAM6s6KLm5B Plan

## Problem Statement and Scope

The review thread reports that missing-HEAD recovery in
`src/awf/runtime/pr_monitor_runner/remote_repair.py` returns after only a
supply-chain policy check when recovery creates a new commit. That can bypass
the pre-commit agent-runtime ownership repair and protected-scope repair path
used by normal monitor-authored dirty commits.

Scope is limited to the missing-HEAD recovery branch in `_commit_dirty_worktree`
and focused unit coverage for that branch.

## Requirements Checklist

- Recovered commits that advance HEAD must run the same pre-commit ownership
  repair gate used by normal dirty-worktree commits before the recovery path
  reports success.
- Recovered commits that advance HEAD must run protected-scope repair when
  monitor repair context (`compose_project` and `compose_file`) is available.
- Recovery must still fail closed when supply-chain policy blocks the recovered
  paths.
- Keep changes minimal and do not alter unrelated monitor repair behavior.

## Implementation Steps

1. Add a focused regression test for missing-HEAD recovery that asserts ownership
   repair and protected-scope repair run before success.
2. Confirm the test fails against the current implementation.
3. Update `_commit_dirty_worktree` recovery branch to run the missing gates
   before returning success for recovered commits.
4. Run the focused test file or narrower selected tests only.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q`

Pass criteria: the focused unit tests pass. Full AWF/GitHub validation is owned
by AWF after agent completion and will not be run inside this agent phase.
