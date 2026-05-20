# Protected Quality Gate Review 4491715538 Validation

Plan reference: `protected_quality_gate_review_4491715538_PLAN.md`

## Requirement Status

- Add or update regression tests before implementation: Complete.
  Added focused regression coverage for raised and unchanged coverage
  `fail_under` handling, plus shared protected-file diff helper behavior.
- Preserve fail-closed behavior for protected file classification: Complete.
  Classifier still emits a violation for all coverage policy edits; the change
  only makes numeric `fail_under` reasons more specific.
- Preserve existing safe deletion/new-file behavior for `git show` missing
  paths: Complete. Shared `git_show_text` retains the missing-path heuristic and
  has a regression test for it.
- Avoid branch switches and pushes; commit locally on the existing AWF branch:
  Complete. No branch or remote operations were used.
- Keep changes narrow and avoid weakening existing quality-gate tests:
  Complete. Existing tests were kept, with additional focused tests.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `src/awf/control/protected_file_diffs.py`
- `src/awf/control/executor.py`
- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/control/test_quality_gates.py`
- `tests/unit/control/test_protected_file_diffs.py`
- `tests/unit/runtime/test_pr_monitor_runner.py`
- `plans/protected_quality_gate_review_4491715538_PLAN.md`
- `plans/protected_quality_gate_review_4491715538_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_pyproject_raising_coverage_fail_under_is_blocked_with_specific_reason tests/unit/control/test_protected_file_diffs.py -q`
  failed initially because `awf.control.protected_file_diffs` did not exist.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_pyproject_lower_coverage_fail_under_is_blocked tests/unit/control/test_quality_gates.py::test_pyproject_raising_coverage_fail_under_is_blocked_with_specific_reason tests/unit/control/test_quality_gates.py::test_pyproject_unchanged_coverage_fail_under_policy_change_is_specific tests/unit/control/test_protected_file_diffs.py -q`
  passed: 6 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py -q`
  passed: 96 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_git_show_text_marks_worktree_safe_directory tests/unit/runtime/test_pr_monitor_runner.py::test_git_show_text_returns_none_for_missing_path tests/unit/runtime/test_pr_monitor_runner.py::test_git_show_text_raises_for_unexpected_git_failure tests/unit/runtime/test_pr_monitor_runner.py::test_protected_status_diff_for_deleted_file_keeps_head_text -q`
  passed: 5 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q`
  passed: 123 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_sync_base_blocks_committed_protected_quality_gate_edits_before_push tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_push_check_allows_safe_pinned_workflow_uses_bump tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_unpushed_commit_protected_scope_detects_rename_source tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_sync_base_protected_scope_diffs_use_remote_branch_base -q`
  passed: 4 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py src/awf/control/executor.py src/awf/runtime/pr_monitor_runner.py tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py tests/unit/runtime/test_pr_monitor_runner.py`
  passed after import ordering was fixed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Remaining Gaps

None.
