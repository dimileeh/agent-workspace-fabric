# PR608 CI Fix Validation

## CI Diagnosis

- Current PR #608 head run passed all eight `python-coverage-shards` jobs.
- `python-full-coverage` failed after combining shard artifacts:
  `Coverage failure: total of 98.97 is less than fail-under=99.00`.
- Downloaded and inspected `full-coverage-report` (`coverage.xml`) instead of
  guessing. The focused gaps addressed here were:
  - `control/executor/planning_conformance.py`: stale artifact cleanup and
    served artifact directory creation failures.
  - `control/executor/execution_validation.py`: unexpected validation cleanup
    guard path that should deposit planning artifacts after successful cleanup.
  - `control/executor/execution_flow.py`: unsupported forge gates, Ollama
    setup failure, validate-only recovery target-head and stale-skip paths,
    push-boundary worktree/CAS edges, and PR head-SHA metadata branches.
  - `control/executor/helpers.py`: agent runtime parsing edge cases.

## Changes Validated

- Added behavior tests for non-fatal conformance artifact cleanup/deposit
  failures and dirty report-parent handling.
- Added behavior tests for validation cleanup guard artifact deposit ordering.
- Added behavior tests for executor lifecycle edges around forge resolution,
  Ollama setup failure, PR push boundary races, PR head SHA handling, and
  validate-only recovery.
- Added a behavior test for helper runtime parsing of enum, valid string,
  invalid string, and non-string values.

## Focused Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_agent_runtime_or_none_parses_supported_values_and_rejects_unknown tests/unit/control/test_planning_ops_branch_edges.py::test_empty_report_parent_residue_treats_oserror_as_dirty tests/unit/control/test_planning_ops_branch_edges.py::test_remove_stale_satisfied_conformance_artifacts_logs_unlink_oserror tests/unit/control/test_planning_ops_branch_edges.py::test_deposit_satisfied_conformance_report_mkdir_oserror_is_non_fatal tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py::test_unexpected_validation_cleanup_guard_deposits_planning_artifacts tests/unit/control/test_executor_parts/test_executor_part_006.py::test_execute_fails_fast_for_unsupported_resolved_forge tests/unit/control/test_executor_parts/test_executor_part_006.py::test_ollama_ensure_failure_stops_before_baseline_and_agent tests/unit/control/test_executor_parts/test_executor_part_006.py::test_profile_resolution_unsupported_forge_fails_before_setup tests/unit/control/test_executor_parts/test_executor_part_006.py::test_missing_worktree_before_pr_push_stops_after_pushing_transition tests/unit/control/test_executor_parts/test_executor_part_006.py::test_start_push_transition_race_stops_before_push tests/unit/control/test_executor_parts/test_executor_part_006.py::test_pr_target_head_update_failure_is_non_fatal_after_open tests/unit/control/test_executor_parts/test_executor_part_006.py::test_pr_open_without_head_sha_skips_target_head_update tests/unit/control/test_executor_parts/test_executor_part_006.py::test_validate_only_recovery_target_head_update_failure_is_non_fatal tests/unit/control/test_executor_parts/test_executor_part_006.py::test_validate_only_recovery_records_stale_skip_after_successful_recheck -q
```

Result: `14 passed in 14.39s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py tests/unit/control/test_executor_parts/test_executor_part_006.py tests/unit/control/test_planning_ops_branch_edges.py
```

Result: `All checks passed!`.

## Coverage Probe

I ran a focused coverage probe over only the new tests to confirm overlap with
the failed CI artifact. This intentionally was not the repository coverage gate.
The probe hit the CI-reported missing source lines:

- `control/executor/execution_flow.py`: 194, 441, 1157, 1158, 1177, 1184,
  1185, 1312; and missing branches at 186, 305, 437, 1176, 1306, 1344, 1415.
- `control/executor/execution_validation.py`: 473.
- `control/executor/planning_conformance.py`: 540, 541, 620, 621, 650, 655,
  661.
- `control/executor/helpers.py`: agent runtime parsing invalid-string branch.

Full AWF/GitHub validation, including the combined coverage gate, remains
managed by AWF after agent completion and was not run locally.
