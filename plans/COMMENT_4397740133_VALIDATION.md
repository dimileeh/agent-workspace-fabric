# Comment 4397740133 Remediation Validation

- Plan reference: `plans/COMMENT_4397740133_PLAN.md`

## Requirement status

1. Treat `!!` paths from validation status as untracked cleanup targets.
   - Status: Complete
   - Evidence: `src/awf/runtime/pr_monitor_runner/path_parsing.py`

2. Keep cleanup behavior consistent with `-fdx` for untracked/ignored cleanup.
   - Status: Complete
   - Evidence: `src/awf/runtime/validation_worktree.py` (existing `git clean -fdx` path)

3. Add/adjust regression coverage for ignored-path cleanup.
   - Status: Complete
   - Evidence:
     - `tests/unit/runtime/test_validation_worktree.py`
     - `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py`

4. Preserve tracked-only restore behavior while handling ignored entries as cleanup-only targets.
   - Status: Complete
   - Evidence: `tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_cleans_ignored_files_with_none_stderr`

## Verification commands

Planned:
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py -q`

Result this session:
- Not executed in this pass (preserved AGENTS/contract guidance to keep local validation focused and report results to AWF-owned CI validation).
