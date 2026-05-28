# PR #301 Review Comment 4567835183 Plan

## Problem Statement And Scope

Address the review-level feedback for PR #301 covering:

- A concern that `ready` workspaces with healthy runtimes might be permanently absorbed by stale recovery if their execution task is lost.
- A helper API mismatch where `_requested_claim_admission_slots(..., claim_limit=N)` ignores `claim_limit` for executor-enabled workers.

Scope is limited to the worker admission/recovery behavior and focused unit coverage. Do not run broad AWF/GitHub-owned validation.

## Requirements Checklist

- Confirm orphaned healthy `ready` workspaces are redispatched by `run_once` when execution capacity is available.
- Preserve the existing stale-recovery guard that prevents healthy queue-waiting `ready` runtimes from being failed as stale active executions.
- Make `_requested_claim_admission_slots` honor `claim_limit` for executor-enabled workers.
- Add focused regression coverage for both review concerns.
- Stage only changed files and commit locally with a conventional commit message tied to comment `4567835183`.

## Implementation Steps

1. Add a focused test proving `run_once` inspects a healthy `ready` runtime and still dispatches it once a slot is available.
2. Add a focused test proving `_requested_claim_admission_slots` caps executor-enabled row slots by `claim_limit`.
3. Run the new tests and confirm the claim-limit test fails before implementation when practical.
4. Update `_requested_claim_admission_slots` to return `min(claim_limit, row_slots)` for executor-enabled workers.
5. Re-run the focused tests and a narrow lint/type check if useful for changed files.
6. Create a validation document against this plan and commit the changed files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
  - Passes all tests in the focused admission regression file.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/claims.py tests/unit/control/test_worker_scheduler_admission.py`
  - Reports no lint errors for changed Python files.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
