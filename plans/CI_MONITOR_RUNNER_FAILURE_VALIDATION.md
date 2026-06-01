# CI Monitor Runner Failure Validation

Plan reference: `plans/CI_MONITOR_RUNNER_FAILURE_PLAN.md`

## Requirement status

- Reproduce the AWF-provided focused failures before editing: Complete.
- Identify the root cause in monitor runner/action logging code without changing protected workflow or gate files: Complete.
- Add or update regression coverage only where needed by the focused failure surface: Complete.
- Preserve real check behavior; do not skip, disable, or weaken tests: Complete.
- Run focused verification for the affected tests only: Complete.
- Commit the fix locally on the current AWF-managed branch without pushing: Complete.

## Evidence

Files changed:
- `src/awf/runtime/pr_monitor_runner/helpers.py`
- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py`
- `tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py`
- `tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py`
- `plans/CI_MONITOR_RUNNER_FAILURE_PLAN.md`
- `plans/CI_MONITOR_RUNNER_FAILURE_VALIDATION.md`

Focused commands:
- Failed before fix: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py::test_monitor_run_transient_status_fetch_preserves_state_operations_and_lifecycle tests/unit/runtime/test_monitor_action_logging.py::TestMonitorActionLogging::test_merge_action_emits_log_line tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_happy_merge_tears_down_compose tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_teardown_raised_exception_swallowed tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_teardown_failure_does_not_mask_completion -q`
- Passed after fix: same command, `5 passed in 9.35s`.
- Passed after fix: `uv run --python 3.12 --extra dev pytest tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestDeferredThreadCapture::test_defer_verdict_captures_and_resolves_then_merges tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestDeferredThreadCapture::test_defer_capture_comment_failure_still_resolves tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py::TestHappyMerge::test_all_green_merges_and_completes tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py::TestAddressComments::test_single_unresolved_thread_addressed_pushed_resolved_then_merged tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py::TestDirtyConflictResolution::test_github_dirty_triggers_cli_conflict_resolve_and_recovery tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py::TestPushRejectRecovery::test_push_rejection_triggers_fetch_and_reset_hard tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py::TestCiFailure::test_failure_triggers_cli_fix_and_push tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py::TestResumePreservesMonitorStartedAt::test_preexisting_started_at_is_reused -q`, `8 passed in 18.14s`.
- Passed after fix: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`, `15 passed in 22.57s`.
- Passed after fix: `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py`, `All checks passed!`.

Discarded command:
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py tests/unit/runtime/test_log_redaction.py::test_redacts_common_secret_shapes -q` failed with pytest collection error because the selected redaction node ID does not exist. No product test failure was observed from that command.

Full AWF/GitHub validation, including full coverage, is intentionally not run locally in the agent phase; AWF owns that broader validation after completion.
