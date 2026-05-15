# Review Thread PRRT_kwDOSJAM6s6CNu6C Validation

Plan reference: `review_thread_PRRT_kwDOSJAM6s6CNu6C_PLAN.md`

## Requirement Status

- Bound callback target validation, including DNS resolution, by the subscription
  delivery timeout: Complete.
- Reuse one per-delivery deadline across validation and the POST attempt so DNS
  time consumes POST budget: Complete.
- Preserve invalid-target handling for `ValueError` validation failures: Complete.
- Treat validation timeout as a delivery/request failure eligible for retry:
  Complete.
- Add focused regression coverage: Complete.

## Evidence

- Changed `src/awf/service/callbacks.py` to start the delivery deadline before
  validation, wrap validation with `asyncio.wait_for`, and pass remaining budget
  into the callback POST.
- Changed `tests/unit/service/test_callbacks.py` with regressions for validation
  timeout handling and validation time consuming POST budget.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_marks_callback_target_validation_timeout_as_request_failure tests/unit/service/test_callbacks.py::test_drain_due_counts_callback_target_validation_against_delivery_timeout -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.
