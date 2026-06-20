# PR614 Shard 8 Line Limit Current Plan

## Problem Statement And Scope

The current PR #614 CI run `27857606326` fails `python-coverage-shards (8)` in
`test_first_party_code_files_stay_under_line_limit`. Three first-party files
exceed the 1,500-line limit:

- `src/awf/control/executor/execution_flow.py` at 1,545 lines
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py` at 1,555 lines
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py` at 1,544 lines

Scope is limited to reducing those files below the existing line limit without
weakening the guardrail or changing unrelated behavior.

## Requirements Checklist

- Keep all work on the current AWF branch; do not push or switch branches.
- Do not edit workflow or quality-gate configuration.
- Preserve behavior covered by the moved/extracted code and tests.
- Bring each oversized file below 1,500 lines.
- Run focused local verification only.

## Implementation Steps

1. Reproduce the failing maintainability test locally.
2. Inspect natural function/test boundaries in each oversized file.
3. Move whole tail tests into new focused part files where imports/fixtures remain clear.
4. Extract a small executor helper from `execution_flow.py` if needed to reduce it below the limit.
5. Run the line-limit guard and any focused moved-test targets.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized files.
- Focused moved-test commands for any split test files.
  - Pass, proving test relocation preserved behavior.

Full AWF/GitHub validation remains owned by AWF after agent completion.
