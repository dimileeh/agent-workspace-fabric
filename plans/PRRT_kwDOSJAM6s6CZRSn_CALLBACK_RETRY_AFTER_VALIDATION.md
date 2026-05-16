# PRRT_kwDOSJAM6s6CZRSn Callback Retry-After Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CZRSn_CALLBACK_RETRY_AFTER_PLAN.md`

## Requirement Status

- Complete: Callback registration 429 responses now have a regression
  assertion for the `Retry-After` header in
  `tests/unit/api/test_callbacks.py`.
- Complete: `_callback_register_rate_limited_response` forwards
  `decision.metadata["retry_after_seconds"]` as the `Retry-After` header,
  with the same fallback shape as workspace admission responses.
- Complete: Existing JSON error body and metadata remain unchanged.

## Evidence

- Initial focused test run failed with missing `Retry-After` header on callback
  429 responses.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  passed: 54 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py tests/unit/api/test_callbacks.py`
  passed.

## Gaps

None.
