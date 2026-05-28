# Review PRRT_kwDOSJAM6s6FidBZ Default Local Admission Plan

## Problem Statement And Scope

The requested-workspace admission counter treats `WorkerConfig.node_id is None`
as `Workspace.node_id IS NULL`, but the surrounding requested-claim and
workspace creation paths use the effective local node id `"local"`. A default
worker can therefore ignore active `node_id="local"` rows when enforcing
`max_concurrent_executions`.

Scope is limited to the requested-admission row-slot counter and focused
regression coverage for default workers. No broad AWF or CI-equivalent
validation will be run in the agent phase.

## Requirements Checklist

- Add a regression proving a default-configured executor worker does not claim
  requested work when an active `node_id="local"` row already consumes its only
  execution slot.
- Preserve existing named-node and remote-node behavior, including null-node
  legacy row accounting.
- Keep the fix scoped to admission row counting.
- Run only targeted validation for the affected test module or selected tests.

## Implementation Steps

1. Add the focused failing regression in
   `tests/unit/control/test_worker_scheduler_admission.py`.
2. Run the selected regression to confirm the current defect when practical.
3. Update `src/awf/control/worker/admission.py` so a default worker counts
   both `node_id="local"` and legacy null-node active rows.
4. Re-run the focused regression and a small neighboring admission test subset.
5. Record evidence in the validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "default_worker_counts_local_node_active_rows or null_node_worker_admission_ignores_active_rows_on_named_nodes or named_node_worker_counts_null_node_provisioning_rows_as_occupied"`

Pass criteria: the selected tests pass, showing the new default-local
regression is fixed while neighboring admission behavior remains intact.
