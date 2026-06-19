# PRRT_kwDOSJAM6s6K6eB- Plan

## Problem Statement and Scope

The protected-scope repair path repairs the shared mirror hooks path before
launching the repair agent and after `AgentRunError`, but a cleanup exception
raised directly by the adapter can skip the post-agent mirror repair. Scope is
limited to making `_repair_protected_scope_changes_before_commit` repair the
mirror after any adapter exception before propagating it.

## Requirements Checklist

- Add a focused regression test for a non-`AgentRunError` adapter cleanup
  exception in protected-scope repair.
- Preserve existing `AgentRunError` provider-recovery behavior.
- Preserve fail-closed handling when mirror hook repair itself fails.
- Keep validation focused; broad AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Add a unit test beside the existing protected-scope mirror repair tests.
2. Confirm the new test fails on the current implementation.
3. Update `_repair_protected_scope_changes_before_commit` to run post-agent
   mirror cleanup before re-raising unexpected adapter exceptions.
4. Run the focused test file or selected tests that cover the changed behavior.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q`

Pass criteria: the focused protected-scope repair tests pass, including the new
cleanup-exception regression.
