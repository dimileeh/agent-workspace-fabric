# PRRT_kwDOSJAM6s6Fjgpx Default Node Stale Recovery Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6Fjgpx_DEFAULT_NODE_STALE_RECOVERY_PLAN.md`

## Requirement Status

- Add a regression proving a default-node worker scans stale `local` active rows
  that occupy admission slots: Complete.
- Update stale recovery candidate selection to use `self._config.node_id or
  "local"`: Complete.
- Preserve legacy null-node admission-slot recovery behavior: Complete.
- Run only focused validation and leave broad AWF/GitHub validation to AWF:
  Complete.

## Evidence

Files changed:

- `src/awf/control/worker/recovery_stale.py`
- `tests/unit/control/test_worker_scheduler_admission.py`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_default_worker_recovers_local_node_provisioning_rows_that_block_admission -q`
  - Failed before the production change with `inspector.calls == []`.
  - Passed after the production change.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::test_named_worker_recovers_null_node_provisioning_rows_that_block_admission -q`
  - Passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/recovery_stale.py tests/unit/control/test_worker_scheduler_admission.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase per the workspace
contract; AWF owns broad validation and merge-gate provenance after completion.
