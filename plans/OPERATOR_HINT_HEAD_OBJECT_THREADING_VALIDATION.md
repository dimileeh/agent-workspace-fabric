# Operator Hint HEAD Object Threading Validation

Plan reference: `plans/OPERATOR_HINT_HEAD_OBJECT_THREADING_PLAN.md`

## Requirement Status

- Preserve the existing `_MonitorHeadObjectMissingError` structured
  `_GitPushResult` behavior for operator hints: Complete.
- Pass the captured `operation_start_head` into `_invoke_cli_for_verdict_result`
  for operator-hint CLI repairs: Complete.
- Pin the threading behavior with a focused regression test: Complete.
- Do not run broad AWF/GitHub-owned validation; use targeted tests only:
  Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/operator_hints.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`
- `plans/OPERATOR_HINT_HEAD_OBJECT_THREADING_PLAN.md`
- `plans/OPERATOR_HINT_HEAD_OBJECT_THREADING_VALIDATION.md`

Focused checks run:

- Failing-first check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "head_object_missing or uses_captured_operation_start_head"`
  failed because `operation_start_head` was not passed to
  `_invoke_cli_for_verdict_result`.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "head_object_missing or uses_captured_operation_start_head"`
  passed with `2 passed, 26 deselected`.

Full AWF/GitHub validation was not run locally per the workspace contract; AWF
owns broad validation, provenance, logs, timeouts, and merge gating after agent
completion.
