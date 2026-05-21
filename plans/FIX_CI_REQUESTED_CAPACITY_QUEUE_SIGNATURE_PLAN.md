# Fix CI Requested Capacity Queue Signature Plan

## Problem Statement And Scope

PR #270 fails the Python full coverage CI check on
`tests/unit/control/test_worker.py::TestRunOnce::test_capacity_claim_empty_queue_returns_empty_result`.
The local focused reproduction shows the test monkeypatch for
`_requested_capacity_queue_signature` no longer matches production's keyword
arguments after the worker began passing a stable `scoring_at` timestamp into the
helper.

Scope is limited to fixing this CI regression without weakening checks,
changing scheduler behavior, switching branches, rebasing, or pushing.

## Requirements Checklist

- Reproduce the reported pytest failure locally before changing code.
- Keep the production capacity scheduler behavior intact.
- Update the stale test double to match the helper contract and cover the
  expected `scoring_at` argument.
- Run focused validation for the failing node ID and nearby queue-signature
  worker tests.
- Save validation evidence in `plans/FIX_CI_REQUESTED_CAPACITY_QUEUE_SIGNATURE_VALIDATION.md`.
- Commit the fix locally with a conventional commit message describing the CI
  check and root cause.

## Implementation Steps

1. Confirm the failing node ID reproduces.
2. Inspect `src/awf/control/worker.py` and the affected test.
3. Update the affected monkeypatched queue-signature helper to accept
   `scoring_at` and assert that the worker supplies it.
4. Re-run the exact failing pytest node.
5. Re-run nearby queue-signature and capacity-claim worker tests that exercise
   the changed contract.
6. Record validation status and commit all scoped changes.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_capacity_claim_empty_queue_returns_empty_result -q`
  - Passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "requested_capacity_queue_signature or capacity_claim_empty_queue" -q`
  - Passes.
