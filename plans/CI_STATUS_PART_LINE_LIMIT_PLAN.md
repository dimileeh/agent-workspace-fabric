# CI Status Part Line Limit Plan

## Problem Statement and Scope

GitHub Actions `python-coverage-shards (8)` fails because
`tests/unit/service/test_status_parts/test_status_part_001.py` has grown to
1544 lines, exceeding the first-party file line limit of 1500 enforced by
`tests/unit/test_core_decomposition_maintainability.py`.

Scope is limited to decomposing that oversized test file along the existing
`test_status_parts` convention. No production behavior, assertions, or quality
gate configuration will be weakened.

## Requirements Checklist

- [ ] Reproduce the failing maintainability check locally.
- [ ] Move a cohesive group of tests out of the oversized status test part.
- [ ] Preserve the moved test behavior and assertions.
- [ ] Keep every first-party code file at or below the 1500-line limit.
- [ ] Run focused validation only; full AWF/GitHub validation remains managed
  after agent completion.
- [ ] Commit the scoped CI fix locally.

## Implementation Steps

1. Create a new sibling status test part for stranded-workspace status tests.
2. Move the stranded-workspace tests from `test_status_part_001.py` into the new
   file with the minimal imports/helpers needed.
3. Run the moved tests directly.
4. Run the targeted line-limit maintainability test.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_status_parts/test_status_part_003.py -q`

Pass criteria: both commands pass. Broad coverage, full unit shards, and
CI-equivalent validation are intentionally left to AWF/GitHub after agent
completion per the workspace contract.
