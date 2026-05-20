# REVIEW_PRRT_kwDOSJAM6s6Di9gL Missing Path Recovery Validation

Plan reference: `REVIEW_PRRT_kwDOSJAM6s6Di9gL_MISSING_PATH_RECOVERY_PLAN.md`

## Requirement Status

- Add a regression test proving `git_show_text` raises when `cat-file` fails for
  a path that is still present in the target ref tree: Complete.
- Preserve recovery for genuinely missing ref paths without relying on English git
  stderr text: Complete.
- Preserve recovery for genuinely missing index paths: Complete.
- Keep unexpected git failures surfaced as `RuntimeError` with original error
  details: Complete.

## Evidence

Files changed:

- `src/awf/control/protected_file_diffs.py`
- `tests/unit/control/test_protected_file_diffs.py`
- `tests/unit/control/test_executor_coverage_edges.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- `tests/unit/runtime/test_monitor_action_logging.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py::test_git_show_text_raises_when_failed_ref_path_still_exists -q`
  failed before implementation with `DID NOT RAISE`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py::test_verify_recovered_post_agent_commit_blocks_protected_rename_source tests/unit/control/test_executor_coverage_edges.py::test_committed_quality_gate_guard_blocks_protected_rename_source tests/unit/control/test_executor_coverage_edges.py::test_staged_protected_file_diffs_treat_deleted_index_path_as_absent tests/unit/runtime/test_pr_monitor_runner.py::test_git_show_text_returns_none_for_missing_path tests/unit/runtime/test_pr_monitor_runner.py::test_git_show_text_raises_for_unexpected_git_failure tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_unpushed_commit_protected_scope_detects_rename_source tests/unit/runtime/test_monitor_action_logging.py::TestMonitorDirtyWorktreeSalvage::test_comment_repair_gets_scope_correction_before_committing_protected_file -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/protected_file_diffs.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_monitor_action_logging.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/protected_file_diffs.py`
  passed.

No remaining gaps.
