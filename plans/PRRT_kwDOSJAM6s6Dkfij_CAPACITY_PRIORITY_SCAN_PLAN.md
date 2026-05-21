# PRRT_kwDOSJAM6s6Dkfij Capacity Priority Scan Plan

## Problem Statement and Scope

The `capacity_queue.blocked_reason_counts` diagnostic is meant to mirror the
local capacity scheduler frontier. `_capacity_queue_candidates` currently orders
requested workspaces by `created_at` before applying
`DEFAULT_CAPACITY_QUEUE_BLOCKER_SCAN_LIMIT`, then the caller reorders the
bounded rows by scheduler score. If newer high-priority workspaces fall beyond
the FIFO bound, metrics can report blockers for the wrong frontier.

Scope is limited to `src/awf/service/metrics.py` and focused regression coverage
in `tests/unit/service/test_metrics.py`.

## Requirements Checklist

- Add a regression proving blocker scans apply scheduler priority before the
  bounded candidate limit.
- Keep the hot-path scan bounded in SQL and preserve the existing latest
  reservation join behavior.
- Reuse the scheduler SQL ordering semantics instead of adding a divergent
  metrics-only ordering implementation.
- Preserve provider recovery filtering and final in-memory ordering semantics.
- Commit only files changed for this review thread.

## Implementation Steps

1. Add a focused test that constrains the blocker scan limit, creates older
   low-priority requested workspaces and a newer high-priority unsatisfiable
   workspace, and expects the high-priority blocker to be counted.
2. Confirm the new test fails against the current FIFO-before-limit query.
3. Update `_capacity_queue_candidates` to order by scheduler class priority,
   effective score, `created_at`, and workspace id before applying `LIMIT`.
4. Pass a single scoring timestamp from `_capacity_queue_blocked_reason_counts`
   into the candidate query so SQL and Python ordering use the same time.
5. Run the focused regression, nearby capacity queue blocker tests, and ruff on
   touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_blocked_reason_counts"`
  - Passes with the new regression included.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`
  - No lint findings.
