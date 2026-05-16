# Request Admission Precheck Plan

## Problem Statement and Scope

PR review comment issue:4460873446 reports that workspace create handlers perform a
non-consuming request-admission pre-check for fresh idempotency keys immediately
before the normal consuming admission call. The consuming call already rejects
exhausted quota without incrementing the counter, so the pre-check adds an
extra worker-thread round trip without changing behavior.

Scope is limited to `POST /v1/workspaces` and `POST /v2/workspaces` request
admission ordering. Durable idempotency replay probes must remain before rate
limiting, and replay responses must continue to bypass quota.

## Requirements Checklist

- Remove redundant `check_request_async` usage from workspace create handlers.
- Preserve rate-limited response shape and `Retry-After` behavior through the
  existing `admit_request_async` decision.
- Preserve durable idempotency replay-before-rate-gate behavior for v1 and v2.
- Add/update a regression test proving fresh idempotency-key creates do not use
  the non-consuming pre-check path.
- Commit only the files changed for this review comment.

## Implementation Steps

1. Add a focused workspace API regression that fails if
   `workspaces.check_request_async` is called during fresh idempotency-key
   creation.
2. Run the focused test to confirm it fails on the current implementation.
3. Remove the redundant pre-check branches, unused helper, and unused import
   from `src/awf/api/routes/workspaces.py`.
4. Run the focused test and relevant workspace API tests.
5. Record validation evidence in `plans/request_admission_precheck_VALIDATION.md`.
6. Stage changed files and create a local conventional commit.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_after_exact_durable_replay_miss -q`
  must fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q`
  must pass after implementation.
