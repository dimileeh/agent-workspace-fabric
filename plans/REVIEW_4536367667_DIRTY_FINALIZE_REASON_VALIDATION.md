# REVIEW_4536367667_DIRTY_FINALIZE_REASON Validation

Plan reference: `plans/REVIEW_4536367667_DIRTY_FINALIZE_REASON_PLAN.md`

## Requirement Status

- Verify the reviewer claim against current code: Complete. The
  dirty-finalize `_MonitorPolicyBlockedError` handler returned
  `_MONITOR_POLICY_BLOCKED_REASON` instead of `exc.reason_code`.
- Add focused regression coverage: Complete. Added a dirty-finalize test where
  `_MonitorPolicyBlockedError` carries `PROTECTED_SCOPE_REPAIR_FAILED`; it
  failed before the implementation change with `MONITOR_POLICY_BLOCKED`.
- Preserve default policy-block behavior and rollback behavior: Complete.
  Existing default policy-block regression still passes.
- Return the exception's `reason_code` from dirty-finalize: Complete.
- Run only targeted validation: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_dirty_finalize_mirror_hooks.py`
- `plans/REVIEW_4536367667_DIRTY_FINALIZE_REASON_PLAN.md`
- `plans/REVIEW_4536367667_DIRTY_FINALIZE_REASON_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_dirty_finalize_mirror_hooks.py::test_pre_push_validation_dirty_finalize_preserves_policy_blocked_exception_reason -q`
  - Before fix: failed because result reason was `MONITOR_POLICY_BLOCKED`.
  - After fix: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py::test_pre_push_validation_finalize_preserves_policy_blocked_reason_code -q`
  - Passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation_dirty_finalize_mirror_hooks.py`
  - Passed.

Full AWF/GitHub validation is managed by AWF after agent completion.
