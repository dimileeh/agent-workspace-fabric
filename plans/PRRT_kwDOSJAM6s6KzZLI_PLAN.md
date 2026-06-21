# PRRT_kwDOSJAM6s6KzZLI Plan

## Problem

Review thread `PRRT_kwDOSJAM6s6KzZLI` reports that
`tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py`
exceeds the first-party file line limit after new tests were added. The local
tree confirms the shard has 1,564 lines, above the 1,500-line maintainability
guard.

## Requirements

- Keep the change scoped to splitting the oversized test shard.
- Preserve the existing tests and assertions; do not weaken behavior coverage.
- Move a coherent subset of tests into an adjacent shard so every touched file
  stays under the 1,500-line limit.
- Run only focused checks that prove the split and moved tests.
- Leave broad AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Run the focused maintainability line-limit guard to confirm the current
   failure when practical.
2. Move the final direct `quality_methods` helper tests from part 009 into an
   adjacent shard.
3. Add only the imports/constants/helpers needed by those moved tests in the
   destination shard.
4. Re-run the focused maintainability guard and touched shard tests.

## Assumptions/Changes

- `test_executor_coverage_edges_part_010.py` is root-owned in this workspace,
  so the split uses a new pytest-discovered
  `test_executor_coverage_edges_part_011.py` shard instead of changing file
  ownership.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_011.py -q`

Full AWF/GitHub validation remains managed by AWF after this agent phase.
