# Protected Repair HEAD Guard Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6K95Ps` reports that
`_repair_protected_scope_changes_before_commit` re-raises unexpected adapter
exceptions after repairing `core.hooksPath` but before verifying that the
worktree `HEAD` object exists in the canonical mirror. If the repair adapter
self-committed with private Git object lookup state before the exception, the
shared ref can remain poisoned for the next monitor attempt.

Scope is limited to the protected-scope repair exception path in
`src/awf/runtime/pr_monitor_runner/remote_repair_protected.py` and a focused
regression test.

## Requirements Checklist

- Verify unexpected non-`AgentRunError` adapter exceptions run the same
  post-adapter `HEAD` object guard before being re-raised.
- Preserve the existing mirror `core.hooksPath` repair behavior.
- Preserve existing `AgentRunError` handling and successful repair behavior.
- Keep changes minimal and avoid broad validation inside the AWF agent phase.

## Implementation Steps

1. Add a focused unit test that simulates an unexpected adapter exception after
   protected-scope repair starts and a missing `HEAD` object.
2. Confirm the test fails before implementation when practical.
3. Refactor the existing post-adapter `HEAD` guard into a small local helper and
   call it from both the successful path and unexpected-exception path.
4. Run only targeted tests for the changed behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py -q`
  - Passes after the fix; the new regression fails before the code change.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per the workspace contract.
