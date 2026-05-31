# Operator Hint Review 4585104324 Plan

## Problem Statement and Scope

Address the review-level feedback from PR comment `issue:4585104324` without
changing AWF-owned branch or push behavior. Scope is limited to the reported
operator remonitor hint and control response concerns:

- Confirm non-pushed operator hint terminal statuses are persisted before the
  runner returns.
- Make workspace control warnings use the explicit response schema type instead
  of relying on Pydantic dict coercion.
- Replace remonitor `assert requested_at is not None` guards with runtime checks
  that remain active under optimized Python.
- Document the intentional concurrent operator hint supersession branch in
  `_merge_concurrent_operator_hint`.

## Requirements Checklist

- [x] Preserve the existing operator hint persistence behavior and regression
  coverage for non-pushed terminal hint statuses.
- [x] `_control_response` and operation-result warning replay use
  `WorkspaceControlWarningResponse` values at the response boundary.
- [x] Remonitor warning payloads remain JSON-serializable in operation results
  and workspace events.
- [x] Remonitor `requested_at` guard failures raise a clear runtime error even
  when Python assertions are disabled.
- [x] Concurrent-hint supersession in the lifecycle merge helper is documented
  where the replacement occurs.
- [x] Run only focused validation commands for the touched behavior.

## Implementation Steps

1. Update focused tests for warning replay to expect typed warning response
   objects while preserving JSON output shape.
2. Update control helper imports, type signatures, and warning conversion.
3. Update remonitor warning construction to use typed warning objects and dump
   them to JSON-compatible dicts for persisted operation/event payloads.
4. Replace the `assert requested_at is not None` guards with an explicit helper
   that raises `RuntimeError` if the invariant is broken.
5. Add a concise lifecycle comment explaining why a newer DB hint may replace
   an in-flight hint in the persisted dictionary.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_helpers.py tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_non_pushed_terminal_status_is_persisted_before_return -q`
  - Passes, proving warning replay and the already-fixed operator hint
    persistence regression.
- `uv run --python 3.12 --extra dev mypy src/awf/service/controls.py src/awf/service/controls_helpers.py src/awf/runtime/pr_monitor_runner/lifecycle.py`
  - Passes with no static typing errors in the touched files.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
