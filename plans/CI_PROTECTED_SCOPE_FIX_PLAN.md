# CI Protected Scope Fix Plan

## Problem Statement and Scope

PR #268 fails the Python full coverage CI job in protected-scope unit tests. The failures cluster around protected file diff collection and PR monitor repair behavior for protected quality-gate files such as workflow files.

Scope is limited to fixing the real protected-scope behavior exercised by the failing tests. This plan does not weaken CI checks, skip tests, alter branch management, or push changes.

## Requirements Checklist

- Reproduce the focused CI failure locally before code changes.
- Preserve literal-path handling while matching expected deleted-index lookup behavior.
- Allow safe workflow action pin bumps without treating unchanged workflow job structure as removed.
- Report committed protected quality-gate edits after failed repair as `PROTECTED_SCOPE_PUSH_BLOCKED` when the protected violation remains.
- Commit a verified protected revert during CI repair when only allowed non-protected edits remain.
- Fail closed when the protected revert baseline cannot be fetched or verified.
- Keep changes narrow and add or preserve regression coverage through the existing failing tests.
- Commit the fix locally with a conventional commit message and do not push.

## Implementation Steps

1. Inspect the failing tests and the implementation paths for staged protected diffs, committed protected diffs, workflow semantic checks, and CI repair commits.
2. Identify the smallest behavioral mismatches causing the five focused failures.
3. Patch the implementation to satisfy the existing regression tests without broad refactors.
4. Re-run the focused repro command.
5. Run additional nearby failing node IDs from the CI summary if the focused failures pass.
6. Run narrow lint/type or broader unit validation only if the touched area warrants it and runtime allows.

## Verification Commands and Pass Criteria

Focused repro:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py::test_staged_protected_file_diffs_treat_deleted_index_path_as_absent tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_push_check_allows_safe_pinned_workflow_uses_bump tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_blocks_committed_protected_quality_gate_edits_after_retry tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_commits_verified_protected_revert_during_scope_repair tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_stops_when_protected_revert_diff_baseline_unavailable -q
```

Pass criteria: all listed tests pass.

Expanded targeted checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_sync_base_protected_scope_diffs_use_remote_branch_base tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_repair_returns_none_when_recheck_fails tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_commit_dirty_worktree_fails_closed_when_protected_revert_check_errors tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_repair_records_remaining_violations_after_agent_failure tests/unit/control/test_executor_validation_fix_cycle.py::TestProtectedQualityGateChanges::test_initial_agent_can_commit_allowed_pyproject_dependency_addition -q
```

Pass criteria: all listed tests pass.
