# CI current shards validation

## Summary

Implemented the scoped CI fix for PR #614 by aligning PR-monitor fake-runner
tests with the current git mirror/head guard command sequence, adding shared
test fixtures so fake-runner tests do not accidentally call host git guard
helpers, and splitting oversized test modules that failed the shard-8
maintainability line-limit guardrail.

No protected workflow or quality-gate configuration files were edited. Full
AWF/GitHub validation and coverage gates remain owned by AWF after agent
completion, per the workspace contract.

## Plan validation

- Preserve AWF branch ownership: satisfied. Work stayed on the current branch;
  no push, rebase, or branch switch was performed.
- Inspect current PR check status: satisfied earlier with `gh pr view` /
  `gh run list`; actionable current failure was shard 8 line-limit guardrail.
- Use focused repros before and after changes: satisfied with the commands
  below.
- Keep changes scoped and do not weaken checks: satisfied. The line-limit
  guardrail is unchanged; oversized files were split at test boundaries.
- Cover changed behavior: satisfied. Existing focused tests now exercise the
  updated PR-monitor guard order and split files directly.

## Focused validation run

- `uv run --python 3.12 --extra dev ruff check <changed Python files>`
  - Result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_runs_profile_coverage_before_push tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_coverage_failure_persists_coverage_reason_code -q`
  - Result: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_action_logging.py::TestMonitorDirtyWorktreeSalvage::test_comment_agent_failure_with_dirty_changes_is_committed_and_resolved tests/unit/runtime/test_monitor_action_logging.py::TestMonitorDirtyWorktreeSalvage::test_comment_repair_gets_scope_correction_before_committing_protected_file -q`
  - Result: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints_state.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_009.py -q`
  - Result: `15 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_021.py -q`
  - Result: `23 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_commit_dirty_worktree_missing_head_recovery_commits_protected_repair_residue tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/control/test_test_quality_guardrails_self.py::test_awf_test_suite_has_no_test_quality_guardrail_violations -q`
  - Result: `3 passed`.

## Residual risk

Broad repository validation, full coverage gates, and GitHub Actions matrix
checks were intentionally not run locally. AWF/GitHub CI will run those after
this agent phase.
