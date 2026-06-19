# PR608 split planning ops shard validation

Plan reference: `plans/PR608_SPLIT_PLANNING_OPS_SHARD_PLAN.md`

## Requirement status

- Verify the referenced file is over the 1,500-line limit: Complete. Initial
  `wc -l` showed `tests/unit/control/test_planning_ops_branch_edges.py` at
  1,509 lines.
- Keep `tests/unit/test_core_decomposition_maintainability.py` unchanged:
  Complete. The guardrail file was read but not edited.
- Move test coverage out of the oversized shard instead of deleting it:
  Complete. The timeout/stdout conformance test moved to
  `tests/unit/control/test_planning_ops_conformance_timeout.py`.
- Keep the moved test focused on the same timeout/stdout behavior: Complete.
  The moved test still asserts that satisfied conformance JSON from idle-timeout
  stdout returns success.
- Run targeted validation only: Complete. Focused commands were run; full
  AWF/GitHub validation remains managed by AWF after agent completion.

## Evidence

- Files changed:
  - `tests/unit/control/test_planning_ops_branch_edges.py`
  - `tests/unit/control/test_planning_ops_conformance_timeout.py`
  - `plans/PR608_SPLIT_PLANNING_OPS_SHARD_PLAN.md`
  - `plans/PR608_SPLIT_PLANNING_OPS_SHARD_VALIDATION.md`
- `wc -l tests/unit/control/test_planning_ops_branch_edges.py
  tests/unit/control/test_planning_ops_conformance_timeout.py
  plans/PR608_SPLIT_PLANNING_OPS_SHARD_PLAN.md`
  - Reviewed shard is now 1,430 lines, below the 1,500-line limit.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py tests/unit/control/test_planning_ops_conformance_timeout.py -q`
  - Passed: 29 tests.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_planning_ops_branch_edges.py tests/unit/control/test_planning_ops_conformance_timeout.py`
  - Passed.
- Attempted planned guard command:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py tests/unit/control/test_planning_ops_conformance_timeout.py tests/unit/test_core_decomposition_maintainability.py -q`
  - Failed on unrelated pre-existing oversized file
    `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py`
    at 1,564 lines.

## Gaps

No gaps remain for review comment `4532002981`. The unrelated oversized file is
outside this scoped fix and was not changed.
