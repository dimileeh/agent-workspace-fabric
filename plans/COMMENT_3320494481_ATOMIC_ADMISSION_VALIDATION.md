# Comment 3320494481 Atomic Admission Validation

Plan reference: `plans/COMMENT_3320494481_ATOMIC_ADMISSION_PLAN.md`

## Requirement Status

- Complete: Added deterministic regressions that force two workers to observe
  the same stale admission precheck and verify only one requested row enters
  `provisioning`.
- Complete: Covered ordinary requested claims and local-capacity requested
  claims in `tests/unit/control/test_worker_scheduler_admission.py`.
- Complete: Requested admission now uses a PostgreSQL transaction advisory lock
  scoped by worker node and recomputes active-row slots in the claim transaction.
- Complete: Existing stale-requested logging remains unchanged for status races;
  capacity queue behavior remains in the local-capacity path after admission.
- Complete: Ran only focused checks for the touched admission behavior.

## Evidence

- Red first: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
  - Failed as expected before implementation:
    `test_concurrent_requested_claims_recheck_admission_slots_atomically` and
    `test_concurrent_local_capacity_claims_recheck_admission_slots_atomically`
    both saw `[1, 1]` dispatches instead of `[0, 1]`.
- Green after implementation: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
  - Passed: `8 passed in 8.83s`.
- Focused lint: `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/admission.py src/awf/control/worker/claims.py src/awf/control/worker/manager.py tests/unit/control/test_worker_scheduler_admission.py`
  - Passed.
- Focused type check: `uv run --python 3.12 --extra dev mypy src/awf/control/worker/admission.py src/awf/control/worker/claims.py src/awf/control/worker/manager.py`
  - Passed.

Full AWF/GitHub validation, coverage gates, and CI-equivalent broad suites were
not run in the agent phase per the AWF workspace contract; AWF owns those after
agent completion.
