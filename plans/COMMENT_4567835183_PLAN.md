# Comment 4567835183 Admission Lock Plan

## Problem Statement

Greptile's review-level comment on PR #301 flagged a mixed-mode admission race:
named workers count active legacy rows with `node_id IS NULL`, but they only hold
the named admission lock. A null-node worker can therefore claim a new active row
under the separate `local` admission lock after the named worker's count and
before the named worker's claim commits.

## Scope

- Keep the existing conservative behavior where named workers count legacy
  `node_id IS NULL` active rows as occupied execution slots.
- Serialize named-worker admission against null-node admission whenever the
  named worker's row count includes null-node rows.
- Preserve per-node serialization for named workers and node-id stamping during
  claims.
- Avoid broad AWF/GitHub-owned validation; run only focused unit tests for the
  admission race.

## Requirements Checklist

- [x] Add a regression test showing named admission waits for the legacy/local
      requested-admission lock before claiming.
- [x] Update requested-admission locking so named workers acquire the legacy
      `local` lock and their named lock in a deterministic order.
- [x] Existing admission behavior remains covered, including provision-only
      bypass and null-node row counting.
- [x] Verification evidence records targeted checks only; full AWF/GitHub
      validation remains post-agent owned.

## Implementation Steps

1. Add a focused PostgreSQL regression that holds the `local`
   requested-admission advisory lock in one transaction and asserts a named
   worker cannot claim until that lock is released.
2. Confirm the new regression fails against the current code.
3. Replace the single-lock helper with ordered requested-admission lock
   acquisition for `local` plus the named node id when `config.node_id` is set.
4. Re-run the new regression and the focused admission test module subset.
5. Save validation results in `plans/COMMENT_4567835183_VALIDATION.md`.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "named_worker_admission_waits_for_null_node_lock"`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "admission or requested_claim"`

Full AWF/GitHub validation remains owned by AWF after agent completion.
