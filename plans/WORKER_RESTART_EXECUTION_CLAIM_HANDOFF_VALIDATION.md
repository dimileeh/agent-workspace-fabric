# Worker Restart Execution Claim Handoff Validation

Plan reference: `WORKER_RESTART_EXECUTION_CLAIM_HANDOFF_PLAN.md`

## Requirement Status

- Complete: Add regression coverage proving a worker-restart recovery claim
  cannot steal a fresh execution lease owned by another worker.
- Complete: Preserve valid handoff behavior when the previous execution claim is
  stale or unset.
- Complete: Preserve idempotent behavior when the same worker refreshes its own
  execution claim.
- Complete: Keep the claim decision atomic for the database-backed executor path
  by checking and writing the claim inside the same transaction.
- Complete: Do not weaken existing safety or validation tests.

## Evidence

Changed files:

- `src/awf/control/executor.py`
- `tests/unit/control/test_executor.py`
- `plans/WORKER_RESTART_EXECUTION_CLAIM_HANDOFF_PLAN.md`
- `plans/WORKER_RESTART_EXECUTION_CLAIM_HANDOFF_VALIDATION.md`

Verification:

- Initial TDD check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py -k "claim_ready_worker_restart_recovery" -q`
  failed before implementation because worker B could steal worker A's live
  execution claim.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py -k "claim_ready_worker_restart_recovery" -q`
  passed with 4 tests.
- Planned test slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py -k claim_ready -q`
  passed with 5 tests.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py tests/unit/control/test_executor.py`
  passed.
