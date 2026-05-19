# Review 4323735631 Validation

Plan reference: `plans/REVIEW_4323735631_PLAN.md`

## Requirement Status

- Verify the findings against current code before changing behavior: Complete.
  `_changed_paths_between_ref_and_head` used `git diff --name-only`, and the workflow reason string used "test-command narrowing" for the introduced-validation-command scenario.
- Add regression coverage proving committed path discovery collects both old and new rename paths from a deterministic null-delimited git diff format: Complete.
  Added `test_changed_paths_between_ref_and_head_includes_rename_sources`.
- Change committed path discovery away from `--name-only` to a rename-safe format: Complete.
  `_changed_paths_between_ref_and_head` now invokes `git diff --name-status -z`.
- Preserve de-duplication and normal path behavior for committed changed paths: Complete.
  The new parser preserves first-seen order, de-duplicates paths, and keeps the prior line-output fallback behavior used by existing fake-runner tests.
- Align the workflow validation-command violation reason with the actual introduced-validation-command case without weakening the gate: Complete.
  Updated the reason to "workflow validation command introduced; introducing validation command is blocked" and kept the violation behavior intact.
- Run focused tests that prove the touched behavior: Complete.
  Focused tests, command-assertion tests, lint, and mypy passed.

## Additional Regression Evidence

Added `test_unpushed_commit_protected_scope_detects_rename_source` to prove a committed rename from `.github/workflows/ci.yml` to `docs/ci.yml` still surfaces the protected source path as a protected-scope violation.

## Commands Run

- Initial red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_changed_paths_between_ref_and_head_includes_rename_sources tests/unit/control/test_quality_gates.py::test_workflow_comment_step_new_validation_command_is_blocked -q`
  Result: failed as expected before implementation.
- Focused final check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_changed_paths_between_ref_and_head_includes_rename_sources tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_unpushed_commit_protected_scope_detects_rename_source tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_blocks_committed_protected_quality_gate_edits_after_retry tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_execute_ci_fix_retries_when_local_commit_touches_protected_scope tests/unit/control/test_quality_gates.py::test_workflow_comment_step_new_validation_command_is_blocked -q`
  Result: passed, 5 tests.
- Broader affected runtime checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_sync_base_blocks_committed_protected_quality_gate_edits_before_push tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_push_check_allows_safe_pinned_workflow_uses_bump tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_sync_base_allows_base_owned_protected_changes_when_base_advances_again tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_blocks_committed_protected_quality_gate_edits_after_retry tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_changed_paths_since_remote_branch_fetches_real_push_remote tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_changed_paths_since_remote_branch_reports_only_local_paths_when_remote_diverged tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_sync_base_protected_scope_resolves_merged_base_before_base_diff tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_sync_base_protected_scope_diffs_use_remote_branch_base -q`
  Result: passed, 8 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py src/awf/control/quality_gates.py tests/unit/runtime/test_pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/control/test_quality_gates.py`
  Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  Result: passed.

## Gaps

None.
