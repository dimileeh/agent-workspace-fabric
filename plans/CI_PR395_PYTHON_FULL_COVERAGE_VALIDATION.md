# CI PR395 Python Full Coverage Validation

Plan reference: `plans/CI_PR395_PYTHON_FULL_COVERAGE_PLAN.md`

## Requirement Status

- Complete: `/readyz` returns structured worker heartbeat failure output when the lookup cannot run. `src/awf/api/routes/health.py` now keeps heartbeat lookup exceptions inside the worker check.
- Complete: `ControlWorker.run_once()` heartbeat/prune maintenance does not crash on non-callable/miswired session factories. `src/awf/control/worker/manager.py` handles `TypeError` in the non-fatal heartbeat maintenance wrappers.
- Complete: Commit-boundary tests isolate ordered-decision behavior from unrelated heartbeat/prune commits. The two affected tests in `tests/unit/control/test_worker_parts/test_worker_part_007.py` stub heartbeat maintenance.
- Complete: Readiness tests focused on Docker/orphans or egress-audit behavior isolate unrelated worker/provider readiness dependencies in `tests/unit/api/test_health_parts/test_health_part_002.py` and `tests/unit/api/test_egress_audit.py`.
- Complete: `tests/unit/api/test_health_parts/test_health_part_001.py` is under the 1500-line maintainability limit after moving task-helper tests to `tests/unit/api/test_health_parts/test_health_part_003.py`.
- Complete: Verification used focused repro/lint commands only. Full AWF/GitHub coverage and CI validation are managed by AWF after agent completion.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_001.py::test_readyz_db_query_failure_returns_503 tests/unit/api/test_health_parts/test_health_part_001.py::test_readyz_db_closed_connection_returns_specific_diagnostic tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_terminal_workspace_with_only_retained_worktree_stays_healthy tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_retains_recent_terminal_worktree_without_failing tests/unit/control/test_worker_parts/test_worker_part_001.py::TestRunOncePart001::test_stale_requested_candidates_are_filtered_before_provision_slot_truncation -q` passed: 5 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_persistent_transient_commit_failure_prevents_dispatch tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_ambiguous_commit_retries_without_duplicate tests/unit/control/test_worker_parts/test_worker_part_046.py::test_run_once_invokes_classified_orphan_reaper_loop tests/unit/api/test_egress_audit.py::test_readyz_includes_egress_audit_check -q` passed: 5 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_003.py -q` passed: 6 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/health.py src/awf/control/worker/manager.py tests/unit/api/test_egress_audit.py tests/unit/api/test_health_parts/test_health_part_001.py tests/unit/api/test_health_parts/test_health_part_002.py tests/unit/api/test_health_parts/test_health_part_003.py tests/unit/control/test_worker_parts/test_worker_part_007.py` passed.

## Gaps

None in the planned scope. Full coverage/CI was not run locally per the AWF workspace contract.
