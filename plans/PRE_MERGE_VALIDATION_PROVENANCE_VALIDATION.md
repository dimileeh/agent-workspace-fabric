# Pre-Merge Validation Provenance Validation

## Result

Implemented. AWF now distinguishes a true validation tier gap from the separate case where the required tier is satisfied but the current PR head has not yet been validated by AWF.

## What Changed

- Added `validation_missing_for_current_head` / `VALIDATION_MISSING_FOR_CURRENT_HEAD`.
- Kept `validation_insufficient_tier` / `VALIDATION_INSUFFICIENT_TIER` for actual tier gaps.
- Updated PR monitor recovery dispatch so current-head provenance gaps create validate-only recovery with the new reason and clearer message.
- Updated validate-only recovery so a successful no-push recovery records the source PR head as `target_head_sha`.
- Updated console formatting so validation reason text says “AWF validation missing for current PR head” instead of exposing the misleading raw tier label.

## Validation

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py::test_auto_merge_dispatches_current_head_validation_recovery_when_tier_is_satisfied tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py::test_sync_feature_pr_recovery_runs_validation_before_monitor_handoff tests/unit/service/test_workspace_response_parts/test_workspace_response_part_001.py::test_workspace_validation_summary_reports_fresh_current_pr_head -q`
  - Passed: 3 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py::test_auto_merge_dispatches_validation_recovery_before_merge tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py::test_auto_merge_dispatches_current_head_validation_recovery_when_tier_is_satisfied tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py::test_auto_merge_clears_docs_scope_stale_after_current_head_validation tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py::test_sync_feature_pr_recovery_runs_validation_before_monitor_handoff tests/unit/service/test_workspace_response_parts/test_workspace_response_part_001.py::test_workspace_validation_summary_reports_fresh_current_pr_head -q`
  - Passed: 5 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_eligibility.py tests/unit/runtime/test_pr_monitor_runner_parts tests/unit/control/test_executor_monitor_recovery_parts tests/unit/service/test_workspace_response_parts -q -n 20`
  - Passed: 245 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_merge_queue_ordering.py tests/unit/service/test_staleness_parts tests/unit/api/test_merge_queue_parts tests/unit/runtime/test_merge_candidate_lifecycle.py tests/integration/test_parallel_candidate_stale_refresh.py -q -n 20`
  - Passed: 118 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `npm --prefix apps/console run lint`
  - Passed.
- `npm --prefix apps/console run typecheck`
  - Passed.
- `npm --prefix apps/console run build`
  - Passed.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Passed.

## Remaining Notes

The state transition remains `monitoring_pr -> ready -> validating -> monitoring_pr` because AWF still reuses the executor for validate-only recovery. The console-facing reason and validation provenance now make that transition explicit as a pre-merge AWF validation provenance pass rather than a tier failure.
