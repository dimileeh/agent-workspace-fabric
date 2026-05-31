# Operator Hint SyncBase Priority Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6F5-BB` reports that `decide()` handles
pending operator hints before base-behind / `BEHIND` / `DIRTY` sync checks. A
behind PR with a pending hint can repeatedly invoke the repair agent, then fail
to push non-fast-forward, without ever selecting `SyncBase`.

Scope is limited to the pure PR monitor decision ordering and its targeted unit
regression.

## Requirements Checklist

- Pending operator hints must not prevent `SyncBase` when `base_behind_count > 0`.
- Pending operator hints must not prevent `SyncBase` when GitHub reports
  `mergeStateStatus == BEHIND` or `DIRTY`.
- Existing terminal-state behavior remains first.
- Operator hints still run before merge once the PR is not behind / dirty.
- Decision-order documentation matches the implemented policy.

## Implementation Steps

1. Add failing `decide()` unit tests covering pending operator hints combined
   with local base-behind, GitHub `BEHIND`, and GitHub `DIRTY`.
2. Move the operator-hint gate after the base-behind / `BEHIND` / `DIRTY`
   `SyncBase` gate, preserving existing pending and needs-human outcomes.
3. Update the `decide()` gate-order docstring and nearby comments.
4. Run targeted unit tests for the touched decision behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py -q`
  must pass.

Full AWF/GitHub validation is managed by AWF after the agent phase and is not
run locally for this thread fix.
