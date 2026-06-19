# Validation for PRRT_kwDOSJAM6s6KCdzX — post-validation conformance deposit ordering

Plan reference: `plans/PRRT_3424139356_PLAN.md`

## Requirement-by-requirement status

- [x] Remove the stale, unreachable deposit block in `execution_validation.py` at lines 702-713, including its misleading comment.
  - Status: **Complete**
  - Evidence: `src/awf/control/executor/execution_validation.py` lines 696-710 now contain only a clarifying comment and no `_deposit_planning_artifacts_best_effort` call on the success path.

- [x] Remove the stale downstream deposit call in `execution_flow.py` at line 1152, including its misleading comment.
  - Status: **Complete**
  - Evidence: `src/awf/control/executor/execution_flow.py` no longer calls `_deposit_planning_artifacts()` immediately after `run_validation_and_fix_cycle`; the success path proceeds directly to reading `validation_result`.

- [x] Keep all existing failure-path deposit behavior intact (those paths still need the deposit before `_mark_failed`).
  - Status: **Complete**
  - Evidence: `_mark_failed_preserving_planning_artifacts` in `execution_validation.py` and `fail_validation_worktree_guard` in `validation_cleanup_guards.py` are unchanged. New regression test `test_validation_conformance_failure_still_deposits_before_mark_failed` asserts that a terminal conformance failure still deposits before marking FAILED.

- [x] Add or update a focused regression test that proves the success path's conformance report is deposited from inside `_run_post_validation_conformance_check`, while the worktree file still exists, and that it survives into the served artifact dir.
  - Status: **Complete**
  - Evidence:
    - Existing test `test_satisfied_post_validation_conformance_stdout_deposits_artifact_before_unlink` (with updated docstring) still passes and asserts the report is deposited before unlink.
    - New test `test_validation_success_path_does_not_redeposit_after_conformance_unlink` asserts that `run_validation_and_fix_cycle` does not invoke `_deposit_planning_artifacts_best_effort` on the success path.

- [x] Run targeted unit tests for the touched executor / planning-ops code and narrow lint/type checks.
  - Status: **Complete**
  - Evidence: see "Verification commands" below.

- [x] Update the plan if scope changes and produce a matching validation document.
  - Status: **Complete**
  - Evidence: this file; no scope changes were required.

## Verification commands run

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py::test_satisfied_post_validation_conformance_stdout_deposits_artifact_before_unlink \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py::test_validation_success_path_does_not_redeposit_after_conformance_unlink \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py::test_validation_conformance_failure_still_deposits_before_mark_failed \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py \
  -q
```

Result: `23 passed`

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/control/executor/planning_ops.py \
  src/awf/control/executor/execution_validation.py \
  src/awf/control/executor/execution_flow.py \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy \
  src/awf/control/executor/planning_ops.py \
  src/awf/control/executor/execution_validation.py \
  src/awf/control/executor/execution_flow.py
```

Result: `Success: no issues found in 3 source files`

## Files changed

- `src/awf/control/executor/execution_validation.py`
- `src/awf/control/executor/execution_flow.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py`
- `plans/PRRT_3424139356_PLAN.md`
- `plans/PRRT_3424139356_VALIDATION.md`

## Gaps

None. Full AWF/GitHub validation (whole-repository test suite, coverage gate, OpenAPI drift check) will be run by AWF after agent completion per workspace contract.
