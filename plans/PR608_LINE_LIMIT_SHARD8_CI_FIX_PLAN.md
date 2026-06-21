# PR608 Line Limit Shard 8 CI Fix Plan

## Problem Statement and Scope

PR #608 fails the `python-coverage-shards (8)` CI job because
`tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py`
has 1505 lines, exceeding the first-party maintainability guard limit of 1500
lines. The `lint-and-type` job also failed before lint/type execution during a
PyPI dependency metadata fetch with a broken-pipe connection error; no local
code failure was visible in that job log.

Scope is limited to splitting the oversized test module while preserving the
existing behavioral assertions and helpers.

## Requirements Checklist

- Keep the current AWF-managed git branch; do not push or rebase.
- Do not edit protected workflow or quality-gate configuration files.
- Reduce the oversized first-party file below the 1500-line limit.
- Preserve the moved test behavior rather than deleting, weakening, or skipping
  tests.
- Run focused verification only: the line-limit guard and the affected moved
  tests.
- Document that broad AWF/GitHub validation remains owned by AWF after agent
  completion.

## Implementation Steps

1. Move the final post-validation conformance report cleanup edge tests from
   `test_executor_coverage_edges_part_002.py` into a new adjacent shard module.
2. Reuse existing helpers from part 002 in the new shard so the split is
   mechanical and low risk.
3. Confirm all affected first-party files are under the line limit.
4. Run the focused maintainability guard and the new shard tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized first-party files.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_013.py -q`
  - Passes the moved behavioral tests.

Full AWF/GitHub validation, broad coverage, and CI-equivalent suites are not run
inside this agent phase per the workspace contract.
