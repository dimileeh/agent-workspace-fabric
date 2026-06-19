# PR614 Shard 8 Line Limit Split Validation

Plan reference: `PR614_SHARD8_LINE_LIMIT_SPLIT_PLAN.md`

## Requirement Status

- Keep all first-party files at or below the 1500-line limit: Complete.
- Preserve existing test behavior by moving tests, not rewriting assertions:
  Complete.
- Add only the minimal imports/helpers needed in new test files: Complete.
- Run focused verification for the line-limit guard and moved tests: Complete.
- Do not run broad AWF/GitHub-owned validation locally: Complete.

## Evidence

Files changed:

- `tests/unit/node/test_git_manager.py`
- `tests/unit/node/test_git_manager_mirror_hooks_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_026.py`
- `plans/PR614_SHARD8_LINE_LIMIT_SPLIT_PLAN.md`
- `plans/PR614_SHARD8_LINE_LIMIT_SPLIT_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_026.py -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check tests/unit/node/test_git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_026.py`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_026.py -q`
  passed.

Full AWF/GitHub validation was not run locally; AWF owns broad validation,
provenance, logs, timeouts, and merge gating after agent completion.
