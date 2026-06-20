# PR614 Shard 8 Part 008 Line Limit Repair Plan

## Problem Statement and Scope

CI run `27862959455` for PR #614 fails in `python-coverage-shards (8)` because
`tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
has 1536 lines and violates
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`.

Scope is limited to decomposing that oversized test file and preserving the
existing assertions.

## Requirements Checklist

- Keep the current AWF-managed branch and do not push.
- Do not edit workflow, quality-gate, or protected configuration files.
- Move complete tests out of the oversized file without weakening assertions.
- Keep all affected first-party code files under the line limit.
- Run focused validation only: the maintainability guard and the moved tests.
- Record that full AWF/GitHub validation remains owned by AWF after completion.

## Implementation Steps

1. Move a coherent tail group of `_repair_operation_start_head_result` tests
   from part 008 into a new numbered part file.
2. Reuse the same local fixture/import style as the existing part files.
3. Run the focused line-limit guard.
4. Run the affected test files only.
5. Commit the scoped fix locally with a conventional commit message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_033.py -q`
  passes.
- Full sharded coverage and broad GitHub checks are not run locally; AWF/GitHub
  owns those gates after this agent phase.
