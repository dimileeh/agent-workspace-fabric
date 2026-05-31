# Issue 346 Validation

Plan reference: `plans/ISSUE_346_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Reuse existing setup-phase behavior for monitor pre-push toolchain availability | Complete | `src/awf/control/executor/monitor_handoff.py` now runs monitor handoff setup via `src/awf/control/executor/monitor_handoff_setup.py` before sync PR monitor handoff. |
| Avoid setup on every pre-push cycle | Complete | Setup is invoked once during monitor handoff; `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` still runs only `post_agent`/`validate` during each pre-push cycle. |
| Classify pure `returncode == 127` as `PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING` | Complete | Added reason code in `pre_push_validation_constants.py`; covered by `test_pre_push_validation_toolchain_missing_bypasses_fix_pass`. |
| Do not consume fix-pass budget for pure toolchain-missing failures | Complete | Pure 127 test asserts one validation call, no adapter calls, no fix commit. |
| Preserve fix-pass behavior for genuine failures | Complete | Existing and new pre-push tests pass; mixed 127/non-127 test verifies the repair prompt uses the real failure. |
| Prefer genuine failures when mixed with 127 failures | Complete | `test_pre_push_validation_mixed_127_prefers_real_failure_for_fix_pass` covers precedence and persisted reason. |
| Include failing command and return code in failure details | Complete | Details assertions added for both toolchain-missing and fix-failed paths. Command text is redacted through `redact_audit_text`. |
| Keep line-count guard intact | Complete | `test_first_party_code_files_stay_under_line_limit` passes after splitting helper/test coverage. |

## Files Changed

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `src/awf/runtime/pr_monitor_runner/pre_push_validation_constants.py`
- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `src/awf/control/executor/monitor_handoff.py`
- `src/awf/control/executor/monitor_handoff_setup.py`
- `src/awf/control/executor/mixins.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_remote_ops.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_005.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`

## Evidence

- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_remote_ops.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_005.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
- Passed: `uv run --python 3.12 --extra dev ruff check .`
- Passed: `uv run --python 3.12 --extra dev ruff format --check .`
- Passed: `uv run --python 3.12 --extra dev mypy`
- Full pytest rerun: `uv run --python 3.12 --extra dev pytest` reached `9139 passed, 7 skipped` and failed on four unrelated tests:
  - `tests/unit/service/test_metrics_parts/test_metrics_part_001.py::test_resource_saturation_reuses_allocation_auxiliary_counts_for_capacity_gate`
  - `tests/unit/service/test_metrics_parts/test_metrics_part_002.py::test_capacity_queue_blocked_reason_counts_caps_provider_suppression_refill_pages`
  - `tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py::test_retry_workspace_errors_and_missing_source_attempt_fallback`
  - `tests/unit/test_postgres_only_edges.py::test_adoption_head_repo_slug_validation_edges`
- Passed direct rerun of the four unrelated full-suite failures:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics_parts/test_metrics_part_001.py::test_resource_saturation_reuses_allocation_auxiliary_counts_for_capacity_gate tests/unit/service/test_metrics_parts/test_metrics_part_002.py::test_capacity_queue_blocked_reason_counts_caps_provider_suppression_refill_pages -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py::test_retry_workspace_errors_and_missing_source_attempt_fallback tests/unit/test_postgres_only_edges.py::test_adoption_head_repo_slug_validation_edges -q`

## Remaining Gaps

No issue #346 requirement remains open. Full-suite `pytest` has unrelated repeated failures outside this change area; the failing tests pass when run directly.
