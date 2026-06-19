# PR608 split planning ops shard plan

## Problem statement and scope

PR review comment `4532002981` reports that
`tests/unit/control/test_planning_ops_branch_edges.py` has grown past the
first-party 1,500-line maintainability limit. The fix is limited to moving the
newly added timeout/stdout conformance test into another focused shard without
changing production behavior or weakening the guardrail.

## Requirements checklist

- Verify the referenced file is over the 1,500-line limit.
- Keep `tests/unit/test_core_decomposition_maintainability.py` unchanged.
- Move test coverage out of the oversized shard instead of deleting it.
- Keep the moved test focused on the same timeout/stdout behavior.
- Run targeted validation only; AWF/GitHub owns broad validation after agent
  completion.

## Implementation steps

1. Create a dedicated planning conformance timeout test module.
2. Move the stdout-timeout adapter and test into that module with only the
   imports/helpers it needs.
3. Remove now-unused imports from the original shard.
4. Confirm the original shard is under 1,500 lines.

## Verification commands and pass criteria

- `wc -l tests/unit/control/test_planning_ops_branch_edges.py`
  - Passes if the file is at or below 1,500 lines.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py tests/unit/control/test_planning_ops_conformance_timeout.py tests/unit/test_core_decomposition_maintainability.py -q`
  - Passes if the moved test and line-limit guard pass.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.

## Assumptions/changes

- The maintainability test was attempted after the split and no longer reports
  `test_planning_ops_branch_edges.py`; it fails on unrelated pre-existing file
  `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py`
  at 1,564 lines. That file is outside this review comment's scope and has no
  local diff, so this fix uses direct `wc -l` evidence for the reviewed shard
  plus focused tests for the moved coverage.
