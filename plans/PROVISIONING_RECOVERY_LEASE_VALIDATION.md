# Provisioning Recovery Lease Validation

Plan reference: `plans/PROVISIONING_RECOVERY_LEASE_PLAN.md`

## Requirement Status

- Add regression coverage for live named-node provisioning rows with no compose
  metadata: Complete.
- Preserve recovery of truly stale provisioning rows that occupy admission slots:
  Complete.
- Apply protection to direct requested claims and local-capacity claims:
  Complete.
- Keep validation focused and leave broad validation to AWF/GitHub: Complete.

## Evidence

Changed files:

- `src/awf/control/worker/claims.py`
- `src/awf/control/worker/dispatch_methods.py`
- `src/awf/control/worker/recovery_stale.py`
- `src/awf/control/worker/helpers.py`
- `tests/unit/control/test_worker_scheduler_admission.py`
- `plans/PROVISIONING_RECOVERY_LEASE_PLAN.md`
- `plans/PROVISIONING_RECOVERY_LEASE_VALIDATION.md`

Focused validation:

- Failing-first check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_live_named_provisioning_claim_is_hidden_from_sibling_stale_scan -q`
  failed because the provisioning row had no execution claim.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_live_named_provisioning_claim_is_hidden_from_sibling_stale_scan -q`
  passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_live_named_provisioning_claim_is_hidden_from_sibling_stale_scan tests/unit/control/test_worker_scheduler_admission.py::test_live_named_capacity_provisioning_claim_is_hidden_from_sibling_stale_scan tests/unit/control/test_worker_scheduler_admission.py::test_default_worker_recovers_local_node_provisioning_rows_that_block_admission tests/unit/control/test_worker_scheduler_admission.py::test_named_worker_recovers_null_node_provisioning_rows_that_block_admission -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/claims.py src/awf/control/worker/dispatch_methods.py src/awf/control/worker/recovery_stale.py src/awf/control/worker/helpers.py tests/unit/control/test_worker_scheduler_admission.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker/claims.py src/awf/control/worker/dispatch_methods.py src/awf/control/worker/recovery_stale.py src/awf/control/worker/helpers.py`
  passed.

Full AWF/GitHub validation, coverage gates, and CI-equivalent checks are managed
by AWF after this agent phase.
