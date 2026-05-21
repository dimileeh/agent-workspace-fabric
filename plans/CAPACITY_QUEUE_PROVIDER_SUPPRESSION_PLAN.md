# Capacity Queue Provider Suppression Plan

## Problem Statement and Scope

`capacity_queue.blocked_reason_counts` currently simulates local capacity gating
for every requested workspace in the local queue. The worker filters
provider-recovery-suppressed workspaces before capacity gating, so operator
metrics can report local capacity saturation that the scheduler would not
enforce during that poll.

Scope is limited to the capacity queue blocked-reason calculation in
`src/awf/service/metrics.py` and focused regression tests.

## Requirements Checklist

- Exclude requested workspaces suppressed by provider recovery `not_before`
  cooldown from capacity blocker counts.
- Exclude requested workspaces suppressed by an open provider/model circuit
  breaker from capacity blocker counts.
- Preserve existing queue totals and planned-resource behavior; only blocker
  counts are filtered.
- Keep scheduler ordering and capacity accumulation behavior unchanged for
  eligible candidates.
- Add regression tests that fail before the metrics fix and pass after it.

## Implementation Steps

1. Add service-level metrics regression tests for provider cooldown suppression
   and open provider/model circuit suppression.
2. Extend capacity queue candidate workspace data with the fields needed to
   evaluate provider/model suppression.
3. Filter capacity blocker candidates through provider cooldown and circuit
   breaker checks before ordering and capacity simulation.
4. Run narrow unit tests for the touched metrics behavior.
5. Run lint/type checks when practical for the touched Python surface.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_metrics.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes, or any unrelated pre-existing failures are documented.
