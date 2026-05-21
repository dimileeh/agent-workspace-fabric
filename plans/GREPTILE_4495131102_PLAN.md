# Greptile 4495131102 Plan

## Problem Statement And Scope

Address the PR #270 outside-diff review comment `issue:4495131102` in the local
AWF workspace. The review identifies a duplicated hot-path database query in the
resource saturation metrics service and asks for an inline comment documenting
local-node scoping for status counts.

## Requirements Checklist

- Add a regression test proving the allocated-resource display path and
  capacity-queue scheduler-gating path share the identical auxiliary allocation
  counts instead of loading them twice.
- Preserve the separate metrics allocation scope and scheduler allocation scope
  total queries; only share the common unreserved workspace count and defaulted
  DinD slot count.
- Keep capacity queue behavior unchanged when no local capacity constraints are
  configured.
- Add a concise inline comment explaining that saturation status counts are
  intentionally scoped to the local node.
- Commit the fix locally with a conventional commit message referencing
  `4495131102`.

## Implementation Steps

1. Add a failing unit test around `summarize_resource_saturation` with capacity
   constraints configured that counts calls to the two shared helper queries.
2. Introduce a small internal value object for allocated-resource auxiliary
   counts and pass it through the metrics and scheduler allocation helpers.
3. Thread the shared counts from `summarize_resource_saturation_for_session`
   into `_capacity_queue_summary` so constrained queue gating can reuse them.
4. Add the local-node scoping comment near the status count query.
5. Run the focused test, then the relevant metrics unit test module if time and
   environment permit.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  must pass, or any environment/setup failure must be documented in validation.
