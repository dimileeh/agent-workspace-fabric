# Review Thread PRRT_kwDOSJAM6s6CLdG1 Plan

## Problem Statement And Scope

The callback delivery service catches broad request exceptions so one failed
operator callback cannot break control-plane work, but the failure path only
stores a bounded exception message on the delivery record. The review asks for a
full traceback diagnostic so request delivery bugs or infrastructure failures
are not masked.

Scope is limited to the non-critical `CALLBACK_REQUEST_FAILED` branch in
`CallbackDeliveryService.drain_due` and focused service tests.

## Requirements Checklist

- Preserve callback retry/failure metadata and bounded database error messages.
- Emit a structured diagnostic for broad request failures that includes a
  traceback.
- Redact common token/credential patterns from the logged traceback.
- Add a regression test proving the traceback log is emitted and secrets are not
  logged.
- Run the focused callback service regression and lint/type checks for the
  touched surface.

## Implementation Steps

1. Add a failing focused test for request-failure traceback logging.
2. Add callback-service logging with a redacted traceback helper.
3. Run the focused test, then relevant lint/type checks.
4. Record validation evidence in
   `plans/review_thread_PRRT_kwDOSJAM6s6CLdG1_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_callback_request_failures_log_redacted_traceback -q`
  fails before implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/service/callbacks.py`
  passes.
