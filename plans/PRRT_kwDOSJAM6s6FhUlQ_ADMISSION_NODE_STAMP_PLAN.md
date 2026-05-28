# PRRT_kwDOSJAM6s6FhUlQ Admission Node Stamp Plan

## Problem Statement And Scope

An unresolved PR review thread reports that a named worker can claim a requested
workspace into `provisioning` without persisting `Workspace.node_id` until the
provisioner success/failure path runs. If that worker crashes after the claim
commits but before provisioner placement metadata is written, the NULL-node
`provisioning` row is counted by named-worker admission but is not recovered by
stale active execution recovery, which filters by the worker node id.

Scope is limited to requested-workspace admission ownership stamping and focused
unit coverage for the direct and local-capacity claim paths.

## Requirements Checklist

- Add regression coverage proving named workers stamp `Workspace.node_id` when
  claiming requested rows into `provisioning`.
- Cover both requested claim paths: direct claim and local-capacity claim.
- Preserve existing admission behavior that counts legacy NULL-node active rows
  for named workers.
- Keep the ownership stamp in the same DB transaction as the requested claim.
- Do not run broad AWF/GitHub validation; record only focused checks.

## Implementation Steps

1. Add focused assertions/helpers in `tests/unit/control/test_worker_scheduler_admission.py`
   for claim-time node ownership.
2. Run the new focused tests and confirm they fail against the current code when
   practical.
3. Update `src/awf/control/worker/claims.py` so successful requested claims set
   `Workspace.node_id` to `self._config.node_id` before the transaction commits.
4. Re-run the focused tests and any directly affected existing admission tests.
5. Create the validation document with requirement-by-requirement evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py::<new direct-claim test> tests/unit/control/test_worker_scheduler_admission.py::<new local-capacity test> -q`
  should fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
  should pass after implementation.
- Full AWF/GitHub validation is intentionally left to AWF after agent completion.
