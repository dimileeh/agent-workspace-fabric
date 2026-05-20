# Worker Restart Execution Claim Handoff Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6DaOTp` reports that executor recovery for
running workspaces with `source == "worker_restart"` overwrites
`execution_claimed_by` and `execution_claim_expires_at` without checking whether
another worker still owns an active execution lease. The scope is limited to the
executor handoff path for preserved worker-restart validation recovery.

## Requirements Checklist

- Add regression coverage proving a worker-restart recovery claim cannot steal
  a fresh execution lease owned by another worker.
- Preserve valid handoff behavior when the previous execution claim is stale or
  unset.
- Preserve idempotent behavior when the same worker refreshes its own execution
  claim.
- Keep the claim decision atomic for the database-backed executor path by
  checking and writing the claim inside the same transaction.
- Do not weaken existing safety or validation tests.

## Implementation Steps

1. Add executor unit tests that seed a running workspace with an active
   worker-restart recovery operation and exercise the claim handoff cases.
2. Introduce a small executor helper that treats a claim as available only when
   it is unset, expired, or already owned by the same worker.
3. Load the running recovery workspace with a row lock before evaluating the
   worker-restart recovery payload and execution claim.
4. Only update `execution_claimed_by` and `execution_claim_expires_at` when the
   helper allows the handoff.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py -k claim_ready -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py tests/unit/control/test_executor.py`
  must pass.
