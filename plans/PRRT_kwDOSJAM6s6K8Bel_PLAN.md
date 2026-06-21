# PRRT_kwDOSJAM6s6K8Bel Plan

## Problem Statement and Scope

The PR review thread reports that `_repair_operation_start_head_result` accepts
a successful `git rev-parse HEAD` result without confirming that the returned
commit object exists in the canonical mirror. Fallback heads already use
`cat-file -e <sha>^{commit}` against the mirror when available.

Scope is limited to the repair operation start-head capture behavior and a
focused regression test.

## Requirements Checklist

- Add a regression test proving a primary `rev-parse HEAD` SHA is not accepted
  when the canonical mirror lacks that commit object.
- Update `_repair_operation_start_head_result` so the primary `rev-parse HEAD`
  path verifies the commit exists before returning it.
- Preserve existing fallback behavior for unavailable or invalid primary heads.
- Run only focused validation for the changed behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a focused unit test near the existing repair start-head tests.
2. Confirm the test fails against current code.
3. Add minimal validation logic to the primary start-head path.
4. Run the focused test(s) that cover the changed helper.
5. Record validation evidence in a validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q -k repair_operation_start_head`
  - Passes after implementation.
  - Fails before implementation for the new regression.
