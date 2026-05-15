# Review 4454403868 Resolved IP Observability Validation

Plan reference: `REVIEW_4454403868_RESOLVED_IP_OBSERVABILITY_PLAN.md`

## Requirement Status

- Complete: Regression coverage proves the rejected resolved IP address is included in callback target validation diagnostics.
- Complete: The existing `CALLBACK_TARGET_INVALID` classification, pending retry status, attempt count, and delivery envelope metadata are preserved.
- Complete: The behavior change is scoped to callback target DNS validation error text.
- Complete: No secrets were added or logged.

## Evidence

Files changed:

- `src/awf/service/callbacks.py`
- `tests/unit/service/test_callbacks.py`

Plan and validation records:

- `plans/REVIEW_4454403868_RESOLVED_IP_OBSERVABILITY_PLAN.md`
- `plans/REVIEW_4454403868_RESOLVED_IP_OBSERVABILITY_VALIDATION.md`

Commands run:

- Failing regression before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_rejects_callbacks_with_private_delivery_target_includes_rejected_ip -q`
  - Failed because the warning message was `target_url resolved host is not public` without `127.0.0.1`.
- Passing focused regression after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_rejects_callbacks_with_private_delivery_target_includes_rejected_ip -q`
- Passing lint:
  - `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
- Passing format check:
  - `uv run --python 3.12 --extra dev ruff format --check tests/unit/service/test_callbacks.py`
- Passing callback service suite:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`

## Gaps

None.
