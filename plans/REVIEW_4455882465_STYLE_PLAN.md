# Review 4455882465 Style Plan

## Problem Statement and Scope

Address the remaining review-level style concerns from PR comment
`issue:4455882465`.

Scope is limited to:

- Replacing the `assert`-based coverage command invariant in
  `_validation_run_command_records` with an explicit runtime guard.
- Removing the now-unreferenced `_looks_like_pytest_node_id` wrapper.

No branch changes, pushes, rebases, or PR comments are in scope.

## Requirements Checklist

- Preserve existing local final coverage command-record behavior.
- Add focused regression coverage for the explicit invariant guard.
- Remove only confirmed dead pytest node-id helper code.
- Run focused validation for the changed areas.
- Create `plans/REVIEW_4455882465_STYLE_VALIDATION.md` documenting status and
  evidence.
- Commit only files changed for this review comment cycle.
- Print the required `AWF-VERDICT` after the fix is complete.

## Implementation Steps

1. Add a failing executor unit test for the missing coverage-command invariant.
2. Replace the assert with an explicit `RuntimeError` guard.
3. Remove `_looks_like_pytest_node_id`.
4. Run focused unit tests plus Ruff and mypy.
5. Record validation evidence and commit locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py::test_validation_command_records_raise_when_coverage_predicate_loses_invariant -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py tests/unit/runtime/test_validation.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py src/awf/runtime/validation.py tests/unit/control/test_executor_coverage_edges.py tests/unit/runtime/test_validation.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
