# PR270 Full Coverage Fix Validation

## Requirement Status

- [x] Identified that `python-full-coverage` failed because total coverage was
  98.41%, below the required 99%; all tests in the reported CI run had passed.
- [x] Added focused regression coverage for uncovered short-circuit and helper
  paths in worker capacity claiming, resource capacity, metrics, service config,
  repository helpers, executor helpers, and quality-gate workflow parsing.
- [x] Kept CI workflow behavior and the 99% coverage threshold unchanged.
- [x] Ran focused validation before broad validation.
- [x] Ran the CI-equivalent full coverage command and restored the coverage gate.

## Command Evidence

- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_worker.py tests/unit/service/test_metrics.py tests/unit/service/test_config.py tests/unit/service/test_resource_capacity.py tests/unit/db/test_workspace_repository.py tests/unit/db/test_scheduler_records.py tests/unit/db/test_repository_coverage.py tests/unit/control/test_quality_gates.py tests/unit/control/test_executor_coverage_edges.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_private_policy_and_expression_helpers_cover_remaining_edges tests/unit/db/test_workspace_repository.py::TestTransition::test_atomic_transition_to_monitoring_pr_stamps_monitor_start tests/unit/db/test_repository_coverage.py::test_repository_replay_key_helpers_short_circuit_non_positive_limits tests/unit/control/test_executor_coverage_edges.py::test_digest_file_and_operation_tier_helpers_cover_error_edges -q`
  - Result: `4 passed in 2.17s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py tests/unit/service/test_config.py tests/unit/service/test_metrics.py::test_allocated_resource_helpers_load_auxiliary_counts_when_missing tests/unit/service/test_metrics.py::test_capacity_metrics_helpers_short_circuit_empty_inputs tests/unit/db/test_workspace_repository.py::TestOwnedPathOverlapLookup::test_scheduler_json_int_expr_handles_unbounded_digits_and_unknown_dialect tests/unit/db/test_workspace_repository.py::TestTransition::test_atomic_transition_to_monitoring_pr_stamps_monitor_start tests/unit/db/test_scheduler_records.py::test_repository_empty_capacity_inputs_short_circuit_without_database tests/unit/db/test_repository_coverage.py::test_owned_path_overlap_match_reports_wildcard_prefix_only_overlap tests/unit/db/test_repository_coverage.py::test_repository_replay_key_helpers_short_circuit_non_positive_limits tests/unit/control/test_quality_gates.py::test_private_policy_and_expression_helpers_cover_remaining_edges tests/unit/control/test_executor_coverage_edges.py::test_digest_file_and_operation_tier_helpers_cover_error_edges tests/unit/control/test_worker.py::TestRunOnce::test_claim_requested_ids_short_circuits_without_database tests/unit/control/test_worker.py::TestRunOnce::test_capacity_claim_empty_queue_returns_empty_result tests/unit/control/test_worker.py::TestRunOnce::test_capacity_private_short_circuit_helpers tests/unit/control/test_worker.py::TestRunOnce::test_capacity_lock_skips_non_postgres_sessions tests/unit/control/test_worker.py::TestRunOnce::test_capacity_decision_signature_helpers_reject_mismatches tests/unit/control/test_worker.py::TestRunOnce::test_earliest_future_datetime_ignores_past_candidate tests/unit/control/test_worker.py::TestRunOnce::test_stale_active_execution_scan_reraises_non_transient_errors tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_age_boost_short_circuits_empty_windows --cov=awf --cov-report=xml:/tmp/pr270-new-tests-coverage.xml --cov-report= --cov-fail-under=0 -q`
  - Result: `145 passed in 14.46s`.
  - Coverage projection from CI artifact plus focused tests: approximately 284
    recovered coverage points against a 274-point deficit.
- `uv run --python 3.12 pytest -n 8 --dist=loadscope --timeout=300 --cov=awf --cov-report=term-missing --cov-report=xml --cov-fail-under=99`
  - Result: `7348 passed, 7 skipped in 672.56s`.
  - Coverage result: `Required test coverage of 99% reached. Total coverage: 99.02%`.

## Gaps

No implementation gaps remain for the reported CI failure. Docker-dependent
integration tests skipped locally because Docker/Compose was unavailable in this
workspace, matching the tests' documented skip behavior.
