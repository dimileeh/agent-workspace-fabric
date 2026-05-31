# PR #339 CI Line Limit and Coverage Plan

## Problem Statement and Scope

PR #339 fails the `python-full-coverage` GitHub Actions job. The failing run
shows:

- `test_first_party_code_files_stay_under_line_limit` rejects two oversized
  first-party files:
  - `src/awf/runtime/pr_monitor_runner/helpers.py` at 1566 lines.
  - `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
    at 1688 lines.
- Total Python coverage is 98.97%, below the required 99.00%.

Scope is limited to code/test decomposition and focused regression coverage for
the PR monitor runner behavior touched by this PR. CI workflow, quality gate,
and protected configuration files are out of scope.

## Requirements Checklist

- Keep the current AWF-managed git branch; do not switch branches, push, rebase,
  or edit protected workflow/configuration files.
- Preserve behavior while reducing oversized first-party files below the
  repository line limit.
- Add focused test coverage for the uncovered PR monitor runner branches causing
  the coverage shortfall.
- Run only targeted local checks relevant to the edited files and behavior.
- Record validation evidence in a matching validation document.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Extract PR monitor git/porcelain path parsing helpers from
   `helpers.py` into a focused module and re-export them from `helpers.py` so
   existing imports remain compatible.
2. Split the oversized `test_pr_monitor_runner_coverage_edges_part_003.py`
   shard by moving the final monitor repair tests into a new coverage edge part
   file with local fixtures.
3. Add targeted tests for the path parsing edge branches that were uncovered in
   CI.
4. Run focused tests:
   - The core maintainability line-limit test.
   - The affected PR monitor runner coverage shard tests.
   - Targeted lint for touched Python files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_009.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check <touched files>` passes.
- Full AWF/GitHub validation is not run locally; AWF owns broad validation after
  agent completion.
