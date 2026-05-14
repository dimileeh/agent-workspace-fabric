# Review 4454403868 Resolved IP Observability Plan

## Problem Statement And Scope

Callback target DNS validation rejects any resolved address that is not public, but the rejection message does not identify which address failed validation. Operators investigating `CALLBACK_TARGET_INVALID` failures need the offending resolved address when a hostname returns mixed public and private records.

Scope is limited to the callback target DNS rejection message and regression coverage in the callback service tests.

## Requirements Checklist

- Add regression coverage proving the rejected resolved IP address is included in callback target validation diagnostics.
- Preserve the existing `CALLBACK_TARGET_INVALID` classification and retry behavior.
- Keep the change scoped to callback target validation observability.
- Avoid logging or committing any secrets.

## Implementation Steps

1. Update the callback service regression test to use mixed public and private resolved addresses and expect the rejected address in diagnostics.
2. Confirm the updated regression fails against the current generic error message.
3. Update `_validate_callback_target_dns` to include the offending address in the `ValueError` message.
4. Run the narrow callback service test and lint/type checks appropriate for the touched Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_rejects_callbacks_with_private_delivery_target -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`

Both commands must pass after implementation. The narrow pytest command should fail after the test-only edit and before the implementation change.
