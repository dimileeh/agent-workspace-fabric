# PRRT_kwDOSJAM6s6GShQ6 Requested Local Fallback Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6GShQ6` reports that the requested-workspace
scheduler fallback for the default `local` worker admits every requested row
with a non-null active reservation node. In a shared database with explicitly
named workers, that lets the local worker list and claim work reserved for a
different Docker host such as `worker-node-a`.

Scope is limited to requested scheduler node scoping in
`src/awf/db/repositories/_scheduler.py` and focused repository/worker
regressions. Existing legacy local hostname adoption must remain covered.

## Requirements Checklist

- Add a regression proving `node_id="local"` does not list or claim a requested
  workspace reserved for a named node.
- Preserve existing behavior that `local` workers can adopt legacy local
  container-hostname reservations.
- Keep named worker scoping behavior unchanged.
- Run only focused checks for the changed files; AWF/GitHub own broad
  validation after agent completion.
- Commit the scoped fix locally with a conventional commit message for the
  review thread.

## Implementation Steps

1. Add repository and worker regressions for a local worker encountering a
   requested workspace reserved for `worker-node-a`.
2. Confirm at least one new regression fails against the current broad fallback.
3. Replace the broad `latest_reservation_node_id IS NOT NULL` local fallback
   with a helper that only matches legacy local container-hostname prefixes.
4. Re-run the new focused tests plus the existing legacy-hostname adoption
   tests.
5. Record focused validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_local_scope_ignores_named_reservation_node -q`
  should fail before the implementation and pass after.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py::TestRunOncePart003::test_non_capacity_local_requested_claim_ignores_named_reservation_node -q`
  should fail before the implementation and pass after.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_local_scope_adopts_legacy_reservation_hostname tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_named_scope_ignores_legacy_reservation_hostname -q`
  should pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py::TestRunOncePart003::test_non_capacity_local_requested_claim_adopts_legacy_reservation_hostname tests/unit/control/test_worker_parts/test_worker_part_003.py::TestRunOncePart003::test_non_capacity_requested_claim_honors_reservation_node -q`
  should pass after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/_scheduler.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/control/test_worker_parts/test_worker_part_003.py`
  should pass.

Full unit suites, coverage gates, frontend builds, and CI-equivalent checks are
intentionally not run in this agent phase.
