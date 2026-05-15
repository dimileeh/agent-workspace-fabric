# Review 4460873446 Replay Miss And Prune Plan

## Problem Statement And Scope

Address remaining issue-style PR comment `4460873446` findings in the current
branch:

- request-admission pruning scans all bucket window sizes but only records the
  caller's window size as pruned;
- workspace and callback routes can treat a positive in-memory idempotency-key
  hit plus durable replay miss as a fresh create path.

Scope is limited to the request-admission limiter, workspace/callback
idempotency replay handling, focused regression tests, and this
plan/validation record.

## Requirements Checklist

- [x] Add a regression proving one prune scan records every bucket window size it
  evaluated, preventing an immediate second scan for another live window size.
- [x] Preserve window-aware stale deletion so live buckets for other window sizes
  are not removed.
- [x] Add workspace v1/v2 regressions proving a known replay-key cache hit plus DB
  miss returns a structured idempotency replay-unavailable error after one
  durable lookup and does not create a new workspace.
- [x] Add a callback regression proving the same known-key durable miss does not
  retry the durable lookup or register a new subscription.
- [x] Implement the smallest scoped changes needed to satisfy those regressions.
- [x] Commit locally on the existing AWF branch without pushing.

## Implementation Steps

1. [x] Add failing tests in `tests/unit/api/test_deps.py`,
   `tests/unit/api/test_workspaces.py`, and `tests/unit/api/test_callbacks.py`.
2. [x] Run the targeted new tests to confirm the current failures.
3. [x] Update `RequestAdmissionLimiter._prune_locked` to mark every scanned bucket
   window size as pruned at that bucket's current window.
4. [x] Add structured replay-unavailable responses for known-key durable misses in
   workspace and callback create paths.
5. [x] Re-run the targeted tests and adjacent API unit modules as time permits.
6. [x] Record validation evidence in
   `plans/REVIEW_4460873446_REPLAY_MISS_PRUNE_VALIDATION.md`.

## Verification Commands And Pass Criteria

- Targeted new tests must fail before implementation and pass after:
  `uv run --python 3.12 --extra dev pytest <new test node ids> -q`.
- Focused module coverage should pass after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py -q`.
- Touched files must pass ruff:
  `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py`.
