# Pre-Push Recovery Anchor Validation

Plan reference: `PRE_PUSH_RECOVERY_ANCHOR_PLAN.md`

## Requirement Status

- Complete: Validate a preferred `operation_start_head` recovery anchor before
  using it.
- Complete: Fall back to the open merge candidate head when the preferred anchor
  is absent or dangling.
- Complete: Preserve existing unrecoverable behavior when no valid recovery
  anchor exists.
- Complete: Add focused regression coverage for the fallback.
- Complete: Run only targeted validation for the changed behavior; AWF/GitHub
  own broad validation after this agent phase.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
- `plans/PRE_PUSH_RECOVERY_ANCHOR_PLAN.md`
- `plans/PRE_PUSH_RECOVERY_ANCHOR_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py::test_pre_push_validation_missing_head_skips_dangling_operation_start_anchor -q`
  - First run failed before implementation because the dangling operation-start
    SHA was used as the recovery anchor.
  - Final run passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -k "missing_head" -q`
  - Passed: `3 passed, 9 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
  - Passed.

No full AWF/GitHub validation suite, full coverage gate, or broad CI-equivalent
command was run inside the agent phase; AWF/GitHub manage that validation after
agent completion.
