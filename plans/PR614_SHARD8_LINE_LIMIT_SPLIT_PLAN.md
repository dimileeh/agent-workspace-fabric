# PR614 Shard 8 Line Limit Split Plan

## Problem Statement and Scope

GitHub Actions run `27848324607`, job `python-coverage-shards (8)`, failed
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`.
The failed guard reported two oversized test files:

- `tests/unit/node/test_git_manager.py`: 1588 lines
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`: 1637 lines

Scope is limited to splitting existing tests across additional focused test
modules without changing production behavior or weakening the guard.

## Requirements Checklist

- Keep all first-party files at or below the 1500-line limit.
- Preserve existing test behavior by moving tests, not rewriting assertions.
- Add only the minimal imports/helpers needed in new test files.
- Run focused verification for the line-limit guard and moved tests.
- Do not run broad AWF/GitHub-owned validation locally.

## Implementation Steps

1. Move `TestRepairMirrorHooksPath` from `tests/unit/node/test_git_manager.py`
   into a new focused node test module.
2. Move the trailing protected-scope revert tests from
   `test_pr_monitor_runner_coverage_edges_part_020.py` into a new part module.
3. Re-run the maintainability line-limit test.
4. Re-run the specific moved test modules only.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes.
- `uv run --python 3.12 --extra dev pytest <new moved-test modules> -q`
  passes.
- Full AWF/GitHub validation remains managed by AWF after agent completion.
