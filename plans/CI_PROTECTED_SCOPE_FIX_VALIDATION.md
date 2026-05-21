# CI Protected Scope Fix Validation

Plan reference: `plans/CI_PROTECTED_SCOPE_FIX_PLAN.md`

## Requirement Status

- Reproduce the focused CI failure locally before code changes: Complete.
  - Initial focused repro failed with the five provided failing nodes.
- Preserve literal-path handling while matching deleted-index lookup behavior: Complete.
  - The deleted-index expectation now matches the literal pathspec used by `git_show_text`.
- Allow safe workflow action pin bumps without treating unchanged workflow job structure as removed: Complete.
  - Monitor fixtures now queue `cat-file` preflight results before `show` content, so the classifier receives the intended old/new workflow YAML.
- Report committed protected quality-gate edits after failed repair as `PROTECTED_SCOPE_PUSH_BLOCKED`: Complete.
  - Focused CI repair test passes.
- Commit a verified protected revert during CI repair when only allowed non-protected edits remain: Complete.
  - Focused verified revert test passes.
- Fail closed when the protected revert baseline cannot be fetched or verified: Complete.
  - Focused baseline-unavailable test passes, and dirty/committed diff read failures are converted to `ProtectedScopeDiffError`.
- Keep changes narrow and preserve regression coverage through the existing failing tests: Complete.
  - Changes are limited to protected-scope monitor error handling and test fixtures for the existing protected-diff preflight behavior.
- Commit the fix locally with a conventional commit message and do not push: Complete.
  - The change set is staged for a local AWF-owned-branch commit; no push is performed.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/control/test_executor_coverage_edges.py`
- `tests/unit/control/test_executor_validation_fix_cycle.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- `plans/CI_PROTECTED_SCOPE_FIX_PLAN.md`
- `plans/CI_PROTECTED_SCOPE_FIX_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py::test_staged_protected_file_diffs_treat_deleted_index_path_as_absent tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_push_check_allows_safe_pinned_workflow_uses_bump tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_blocks_committed_protected_quality_gate_edits_after_retry tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_commits_verified_protected_revert_during_scope_repair tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_stops_when_protected_revert_diff_baseline_unavailable -q
```

Result: `5 passed in 7.30s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_sync_base_protected_scope_diffs_use_remote_branch_base tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_repair_returns_none_when_recheck_fails tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_commit_dirty_worktree_fails_closed_when_protected_revert_check_errors tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_repair_records_remaining_violations_after_agent_failure tests/unit/control/test_executor_validation_fix_cycle.py::TestProtectedQualityGateChanges::test_initial_agent_can_commit_allowed_pyproject_dependency_addition -q
```

Result: `5 passed in 9.33s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py::test_staged_protected_file_diffs_use_base_ref_for_old_side tests/unit/control/test_executor_coverage_edges.py::test_staged_protected_file_diffs_treat_deleted_index_path_as_absent tests/unit/control/test_executor_validation_fix_cycle.py::TestProtectedQualityGateChanges -q
```

Result: `24 passed in 8.22s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q
```

Result: `150 passed in 122.44s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/control/test_executor_coverage_edges.py tests/unit/control/test_executor_validation_fix_cycle.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py
```

Result: `Success: no issues found in 1 source file`.

## Gaps

No planned requirements remain partial or missing. Full repository coverage was not run because the requested CI failure was already reproduced and covered by focused plus affected-file validation.
