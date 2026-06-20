# Fix Pass HEAD Reason Validation

Plan reference: `plans/FIX_PASS_HEAD_REASON_PLAN.md`

## Requirement Status

- Add a focused regression test that fails when a fix-pass
  `_MonitorHeadObjectMissingError` reason code is flattened: Complete.
- Update the fix-pass missing-HEAD handler to return `exc.reason_code`:
  Complete.
- Keep existing messages and unrelated exception handling unchanged: Complete.
- Run only targeted validation for the changed behavior: Complete.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
- `plans/FIX_PASS_HEAD_REASON_PLAN.md`
- `plans/FIX_PASS_HEAD_REASON_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k head_object_missing`
  - First run after adding the regression failed because the result reason was
    `HEAD_OBJECT_MISSING_UNRECOVERABLE` instead of
    `HEAD_OBJECT_MISSING_FIX_PASS_CUSTOM`.
  - Re-run after the implementation change passed:
    `1 passed, 16 deselected`.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run inside this agent phase.
