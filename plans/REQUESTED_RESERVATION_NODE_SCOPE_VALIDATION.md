# Requested Reservation Node Scope Validation

Plan reference: `plans/REQUESTED_RESERVATION_NODE_SCOPE_PLAN.md`

## Requirement Status

- Complete: Requested scheduler candidate queries with a `node_id` now use the effective requested placement `COALESCE(Workspace.node_id, latest active ResourceReservation.node_id)`.
- Complete: A requested workspace reserved for node A is not listed for node B; the regression initially failed because node B's row appeared in node A's candidate set.
- Complete: A requested workspace reserved for the current node remains listable before `Workspace.node_id` is stamped.
- Complete: Already-stamped workspace node filtering is preserved by preferring `Workspace.node_id` over the reservation node.
- Complete: Verification used focused local checks only. Full AWF/GitHub validation is managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/db/repositories/_scheduler.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`

Focused checks:

- Initial TDD failure: `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_scopes_null_workspace_node_to_active_reservation_node -q`
  - Failed before implementation because the node-B reserved workspace was listed for node A.
- Passing regression and neighboring SQL-shape check: `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_postgres_scheduler_workspace_rows_can_scope_to_node_id tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_scopes_null_workspace_node_to_active_reservation_node -q`
  - Passed: `2 passed`.
- Narrow lint: `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/_scheduler.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`
  - Passed.
- Narrow type check: `uv run --python 3.12 --extra dev mypy src/awf/db/repositories/_scheduler.py`
  - Passed.

## Gaps

No planned requirement gaps remain. Full-suite validation, coverage, CI provenance, and merge gating are intentionally left to AWF/GitHub after this agent phase.
