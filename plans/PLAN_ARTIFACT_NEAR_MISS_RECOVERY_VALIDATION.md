# AWF Planning Artifact Near-Miss Recovery Validation

## Summary
Implemented a narrow recovery path for single typo-like ignored planning
artifacts under `docs/awf-plans/`. The existing exact required-plan check remains
authoritative, and unsafe or ambiguous cases continue to fail with
`AGENT_PLAN_PHASE_SCOPE_VIOLATION`.

## Validation Commands
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py tests/unit/control/test_planning_ops_branch_edges.py -q`
  - Result: `49 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py src/awf/control/executor/planning_scope.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py tests/unit/control/test_planning_ops_branch_edges.py`
  - Result: `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py src/awf/control/executor/planning_scope.py`
  - Result: `Success: no issues found in 2 source files`

## Coverage Notes
- Correct ignored required plan files still pass via the existing digest fallback.
- A single safe near-miss markdown artifact is moved to the required path.
- Multiple candidates, source changes, distant names, non-candidate files, and
  existing required plan files are refused with preserved scope-violation
  semantics and near-miss evidence where applicable.
