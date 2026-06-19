# PR614 Shard 8 Part 008 Split Validation

Plan reference: `plans/PR614_SHARD8_PART008_SPLIT_PLAN.md`

## Requirement Status

- Confirm the line-limit guard fails locally with the focused maintainability test: Complete.
- Move enough tests from part 008 into a new adjacent part file so every first-party file is under 1500 lines: Complete.
- Preserve the moved tests' behavior and assertions: Complete.
- Run focused verification for the moved tests and the line-limit guard: Complete.
- Do not run full AWF/GitHub-owned broad validation locally: Complete.

## Evidence

Files changed:

- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_029.py`
- `plans/PR614_SHARD8_PART008_SPLIT_PLAN.md`
- `plans/PR614_SHARD8_PART008_SPLIT_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Before split: failed with part 008 at 1554 lines.
  - After split: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_029.py -q`
  - Passed.
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_029.py`
  - Passed.

Full AWF/GitHub validation was not run locally; AWF owns broad validation after agent completion.
