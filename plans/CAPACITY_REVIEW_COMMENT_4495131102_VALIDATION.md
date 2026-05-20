# Capacity Review Comment 4495131102 Validation

Plan reference: `CAPACITY_REVIEW_COMMENT_4495131102_PLAN.md`

## Requirement Status

- Add regression coverage that fails when the capacity scheduler can see requested workspaces pinned to another node: Complete.
- Add regression coverage that metrics reservation totals are obtained through the repository-owned aggregation path while preserving workspace-node routing semantics: Complete.
- Add regression coverage that `_record_capacity_queue_decision` logs a warning when no `TaskAttempt` exists: Complete.
- Implement the smallest code changes needed to satisfy those tests: Complete.
- Preserve existing single-node and legacy `NULL` node behavior: Complete.
- Run focused unit tests covering scheduler, repository/metrics, and missing-attempt behavior: Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `src/awf/db/repositories.py`
- `src/awf/service/metrics.py`
- `tests/unit/control/test_worker.py`
- `tests/unit/db/test_workspace_repository.py`
- `tests/unit/db/test_scheduler_records.py`
- `tests/unit/api/test_metrics_capacity.py`

Failing-before evidence:

- `tests/unit/db/test_scheduler_records.py::test_resource_reservation_active_latest_totals_for_workspace_scope_uses_workspace_node` failed because the repository method was missing.
- `tests/unit/api/test_metrics_capacity.py::test_active_latest_totals_for_workspace_scope_delegates_to_repository` failed because metrics executed local SQL instead of delegating.
- `tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_scans_only_workspaces_for_worker_node` failed because a remote-node requested workspace received a capacity queue decision.
- `tests/unit/control/test_worker.py::TestRunOnce::test_capacity_queue_decision_warns_when_attempt_is_missing` failed because no warning was logged.

Passing verification:

- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/db/repositories.py src/awf/service/metrics.py tests/unit/control/test_worker.py tests/unit/db/test_workspace_repository.py tests/unit/db/test_scheduler_records.py tests/unit/api/test_metrics_capacity.py`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestOwnedPathOverlapLookup::test_postgres_scheduler_workspace_rows_can_scope_to_node_id tests/unit/db/test_workspace_repository.py::TestOwnedPathOverlapLookup::test_postgres_scheduler_workspace_rows_apply_candidate_limit tests/unit/db/test_workspace_repository.py::TestOwnedPathOverlapLookup::test_postgres_scheduler_lists_skip_locked_rows -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_defers_for_unreserved_active_local_workspace tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_ignores_unreserved_active_workspace_on_other_node tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_scans_only_workspaces_for_worker_node tests/unit/control/test_worker.py::TestRunOnce::test_capacity_queue_decision_warns_when_attempt_is_missing tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_bounds_fully_blocked_page_scan -q`

## Remaining Gaps

None.
