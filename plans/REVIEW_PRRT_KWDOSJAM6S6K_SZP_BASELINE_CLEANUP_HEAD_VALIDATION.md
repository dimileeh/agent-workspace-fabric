# Baseline Cleanup Missing HEAD Recovery Validation

Plan reference:
`plans/REVIEW_PRRT_KWDOSJAM6S6K_SZP_BASELINE_CLEANUP_HEAD_PLAN.md`

## Requirement Status

- Verify the actual baseline cleanup handler does not already perform missing
  HEAD verification/recovery: Complete. The handler repaired mirror hooks and
  re-raised `ComposeExecCleanupError` without calling
  `verify_head_object_exists` or missing-HEAD recovery.
- Add missing-HEAD verification/recovery before re-raising baseline
  `ComposeExecCleanupError`: Complete. `execution_flow.py` now runs a
  baseline-specific recovery helper after mirror hook repair and before
  re-raising to the outer cleanup-failure path.
- Preserve existing cleanup-failure behavior: Complete. The handler still
  repairs mirror hooks first and still re-raises so the existing outer
  `EXEC_PROCESS_CLEANUP_FAILED` failure path owns the terminal workspace state.
- Add focused regression coverage for the baseline cleanup branch: Complete.
  `tests/unit/control/test_executor_baseline_cleanup_recovery.py` asserts HEAD
  verification, recovery arguments, skipped agent execution, and final cleanup
  failure status.
- Run only targeted tests/checks: Complete. Full AWF/GitHub validation was not
  run in the agent phase and remains managed by AWF after completion.

## Evidence

- Changed `src/awf/control/executor/execution_flow.py`.
- Added `tests/unit/control/test_executor_baseline_cleanup_recovery.py`.
- Added this plan/validation pair.

Targeted commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_baseline_cleanup_recovery.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py::test_execute_repairs_mirror_hooks_path_after_baseline_coverage_cleanup_failure -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_flow.py tests/unit/control/test_executor_baseline_cleanup_recovery.py
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_setup_cleanup_recovery.py -q
```

All targeted commands passed.
