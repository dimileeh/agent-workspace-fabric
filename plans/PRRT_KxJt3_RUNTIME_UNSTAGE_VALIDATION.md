# PRRT_KxJt3 Runtime Unstage Validation

Plan reference: `plans/PRRT_KxJt3_RUNTIME_UNSTAGE_PLAN.md`

## Requirement Status

- Verify the review claim against current code: Complete.
  - `remote_repair.py` used `git rm --cached` for staged runtime-root paths.
- Add a regression test showing runtime-root paths are unstaged with literal
  pathspecs rather than removed from the index: Complete.
  - Added
    `test_recover_missing_head_object_unstages_runtime_paths_without_deletion`.
- Replace `git rm --cached` with a non-destructive index unstage command:
  Complete.
  - `remote_repair.py` now calls
    `git --literal-pathspecs reset -q HEAD -- <excluded paths>`.
- Run targeted tests only: Complete.
  - Full AWF/GitHub validation was not run in-agent; AWF owns broad validation
    after agent completion.
- Commit the focused fix locally: Complete.
  - The local commit for this fix cycle includes this validation record.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
- `plans/PRRT_KxJt3_RUNTIME_UNSTAGE_PLAN.md`
- `plans/PRRT_KxJt3_RUNTIME_UNSTAGE_VALIDATION.md`

Focused commands:

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q -k recover_missing_head_object`
  - Result: failed as expected because the literal-pathspec reset command was
    absent.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q -k recover_missing_head_object`
  - Result: passed, `3 passed, 27 deselected`.
- After implementation:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
  - Result: passed.

## Remaining Gaps

None for the planned scope. Broad validation remains AWF/GitHub-owned per the
workspace contract.
