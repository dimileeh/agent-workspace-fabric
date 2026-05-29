# PRRT_kwDOSJAM6s6Fjgpx Default Node Stale Recovery Plan

## Problem Statement And Scope

PR #301 review thread `PRRT_kwDOSJAM6s6Fjgpx` reports that requested-admission
slot accounting normalizes an unset worker node id to `local`, but stale active
execution recovery still filters with `self._config.node_id` directly. A stale
`node_id='local'` provisioning row can therefore occupy admission capacity while
the default worker recovery scan misses it.

Scope is limited to stale active execution candidate selection and focused
regression coverage.

## Requirements Checklist

- Add a regression proving a default-node worker scans stale `local` active rows
  that occupy admission slots.
- Update stale recovery candidate selection to use the same effective node id as
  admission accounting: `self._config.node_id or "local"`.
- Preserve legacy null-node admission-slot recovery behavior for existing rows.
- Run only focused validation for the changed behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a focused regression in `tests/unit/control/test_worker_scheduler_admission.py`.
2. Run the new test and confirm it fails before the code change.
3. Update `src/awf/control/worker/recovery_stale.py` candidate filtering.
4. Re-run the focused regression and a nearby stale-recovery admission test.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_default_worker_recovers_local_node_provisioning_rows_that_block_admission -q`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_named_worker_recovers_null_node_provisioning_rows_that_block_admission -q`
  - Passes to confirm legacy null-node recovery behavior remains intact.
