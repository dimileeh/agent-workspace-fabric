# Review PRRT_kwDOSJAM6s6F7DSu Quoted Porcelain Paths Validation

Plan reference: `plans/review_PRRT_kwDOSJAM6s6F7DSu_quoted_porcelain_paths_PLAN.md`

## Requirement Status

- Add a regression test showing a deterministic autofix retry restages a path
  whose porcelain status path is C-quoted because it contains spaces:
  Complete.
- Normalize quoted porcelain paths before safety set comparisons and before
  passing restage paths to `git add`: Complete.
- Preserve existing unsafe-path checks and bounded restaging behavior:
  Complete.
- Run only targeted validation for the changed runtime tests, with broad
  AWF/GitHub validation left to AWF after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/commit_autofix.py`
- `src/awf/runtime/pr_monitor_runner/helpers.py`
- `tests/unit/runtime/test_pr_monitor_commit_autofix.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_restages_quoted_porcelain_paths -q`
  failed before the implementation with quoted status paths remaining dirty,
  then passed after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  passed with 17 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py::test_git_push_and_porcelain_helpers_cover_clean_rename_and_invalid_lines -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/commit_autofix.py src/awf/runtime/pr_monitor_runner/helpers.py tests/unit/runtime/test_pr_monitor_commit_autofix.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/commit_autofix.py src/awf/runtime/pr_monitor_runner/helpers.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase per workspace
contract; AWF owns broad validation and merge gating after completion.
