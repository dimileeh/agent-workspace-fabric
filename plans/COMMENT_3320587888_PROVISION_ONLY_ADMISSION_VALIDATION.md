# Comment 3320587888 Provision-Only Admission Validation

Plan reference: `COMMENT_3320587888_PROVISION_ONLY_ADMISSION_PLAN.md`

## Requirement Status

- Complete: Added regressions proving a no-executor worker with
  `max_concurrent_executions=0` can claim and provision requested work.
- Complete: Preserved the execution-slot row admission gate for workers that
  have an executor by routing only no-executor workers around that gate.
- Complete: Kept local-capacity and non-local-capacity requested claim paths
  consistent by using the same claim-admission slot helper in both paths.
- Complete: Ran focused validation only; full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

- Changed `src/awf/control/worker/claims.py`.
- Changed `tests/unit/control/test_worker_scheduler_admission.py`.
- Initial TDD failure:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "provision_only"`
  failed with both new provision-only tests returning `0` dispatches instead
  of `1`.
- Focused regression pass:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "provision_only"`
  passed with `2 passed, 8 deselected`.
- Focused admission module pass:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
  passed with `10 passed`.
- Touched-file lint pass:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/claims.py tests/unit/control/test_worker_scheduler_admission.py`
  passed.

## Remaining Gaps

None for the planned scope. Broad repository validation, coverage gates, and CI
provenance were intentionally not run inside the agent phase per the AWF
workspace contract.
