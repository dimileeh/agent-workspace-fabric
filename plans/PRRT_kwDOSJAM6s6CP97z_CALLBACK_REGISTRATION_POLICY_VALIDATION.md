# PRRT_kwDOSJAM6s6CP97z Callback Registration Policy Validation

Plan reference: `PRRT_kwDOSJAM6s6CP97z_CALLBACK_REGISTRATION_POLICY_PLAN.md`

## Requirement Status

- Complete: `POST /v1/callbacks` rejects `http://` targets when callbacks require HTTPS.
  - Evidence: `tests/unit/api/test_callbacks.py::test_register_callback_rejects_http_target_when_https_required_without_insert`
- Complete: `POST /v1/callbacks` rejects non-allowlisted hosts when callback allowed hosts are configured.
  - Evidence: `tests/unit/api/test_callbacks.py::test_register_callback_rejects_non_allowlisted_target_without_insert`
- Complete: Rejected registration requests do not create callback subscription rows.
  - Evidence: both new tests assert `_subscription_count(engine) == 0`.
- Complete: Delivery-time validation remains in place for legacy or manually edited rows.
  - Evidence: `tests/unit/service/test_callbacks.py` passed unchanged delivery validation coverage.
- Complete: Idempotency conflict behavior and existing URL/event schema validation are preserved.
  - Evidence: `tests/unit/api/test_callbacks.py` passed.

## Commands Run

- Failed before implementation as expected:
  - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rejects_http_target_when_https_required_without_insert tests/unit/api/test_callbacks.py::test_register_callback_rejects_non_allowlisted_target_without_insert -q`
  - Result: both tests failed with `201 == 422`.
- Passed after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rejects_http_target_when_https_required_without_insert tests/unit/api/test_callbacks.py::test_register_callback_rejects_non_allowlisted_target_without_insert -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py src/awf/service/callbacks.py tests/unit/api/test_callbacks.py`
  - `uv run --python 3.12 --extra dev mypy src/awf`

## Gaps

None.
