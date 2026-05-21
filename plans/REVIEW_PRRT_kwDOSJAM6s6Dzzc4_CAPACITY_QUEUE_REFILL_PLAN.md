# Capacity Queue Provider Suppression Refill Plan

## Problem Statement and Scope

The capacity queue blocker diagnostic currently fetches a bounded scheduler frontier and then removes provider-recovery suppressed candidates. If cooldown or open-circuit rows dominate the head of the queue, the diagnostic can analyze fewer than the intended number of eligible candidates and miss later workspaces that are capacity-blocked.

Scope is limited to `src/awf/service/metrics.py` capacity queue blocker candidate loading and focused unit regression coverage in `tests/unit/service/test_metrics.py`.

## Requirements Checklist

- Preserve scheduler-order candidate selection for capacity queue blocker diagnostics.
- Refill the blocker candidate scan after provider cooldown or open-circuit suppression removes candidates, up to the configured scan limit or queue exhaustion.
- Keep provider-suppressed candidates excluded from blocker counts.
- Add regression coverage for a suppressed queue head followed by an eligible capacity-blocked workspace.
- Run focused tests for the changed metrics behavior.
- Commit only files changed for this thread with a conventional commit message referencing the review thread id.

## Implementation Steps

1. Add a failing unit test that sets a small blocker scan limit, creates provider-suppressed high-priority requested workspaces ahead of an eligible requested workspace, and expects the eligible workspace's capacity blocker to be counted.
2. Update the capacity queue metrics candidate path to page through scheduler-ordered candidates and append provider-recovery eligible rows until the scan limit is filled or no more rows exist.
3. Keep the existing single-page behavior for queues without suppression and preserve the existing provider cooldown/open-circuit filtering helper.
4. Re-run the focused test and the surrounding capacity queue metrics tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_blocked_reason_counts"`

Pass criteria: the new regression and existing capacity queue blocker tests pass.
