# Callback Review 4454403868 Validation

Plan reference: `plans/CALLBACK_REVIEW_4454403868_PLAN.md`

## Requirement Status

- Declare the structured `422` callback registration response in OpenAPI:
  Complete.
  - `src/awf/api/routes/callbacks.py` declares a structured
    `HTTPExceptionErrorResponse` for `POST /v1/callbacks` policy rejections.
  - `openapi.json` was regenerated and now includes the structured callback
    registration `422` response.
- Preserve existing retry behavior for callback delivery failures: Complete.
  - Both new delivery-code paths still call `CallbackDeliveryRepository.mark_failed_or_retry`
    with the subscription backoff settings.
- Store a distinct code for delivery budget exhaustion after successful target
  validation: Complete.
  - `src/awf/service/callbacks.py` records
    `CALLBACK_DELIVERY_BUDGET_EXCEEDED` through a dedicated helper when
    validation completes but no POST budget remains.
- Store a distinct code for runtime callback target policy violations: Complete.
  - Delivery-time HTTPS and callback host allowlist failures now record
    `CALLBACK_TARGET_POLICY_VIOLATION`.
- Keep structural malformed URL, private IP, DNS, NAT64, and 6to4 rejection
  behavior under the existing invalid-target path unless tests prove otherwise:
  Complete.
  - Only configurable HTTPS/allowlist checks raise the new policy-violation
    subclass.
  - Existing invalid-target tests continue to cover resolved private IP, NAT64,
    and 6to4 rejection.
- Add or update focused regression tests before implementation where practical:
  Complete.
  - Updated the OpenAPI and service expectations first, then confirmed the
    targeted tests failed before implementation.
- Regenerate or verify `openapi.json` when the spec changes: Complete.

## Evidence

Files changed:

- `src/awf/api/routes/callbacks.py`
- `src/awf/service/callbacks.py`
- `tests/unit/api/test_openapi_artifact.py`
- `tests/unit/service/test_callbacks.py`
- `docs/REST_API_REFERENCE.md`
- `openapi.json`
- `plans/CALLBACK_REVIEW_4454403868_PLAN.md`
- `plans/CALLBACK_REVIEW_4454403868_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_callback_endpoints_document_structured_error_responses tests/unit/service/test_callbacks.py::test_drain_due_records_budget_exceeded_when_validation_consumes_timeout_budget tests/unit/service/test_callbacks.py::test_drain_due_enforces_https_only_callback_target_policy tests/unit/service/test_callbacks.py::test_drain_due_enforces_callback_target_allowlist_policy -q`
  - Failed before implementation as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_callback_endpoints_document_structured_error_responses tests/unit/service/test_callbacks.py::test_drain_due_records_budget_exceeded_when_validation_consumes_timeout_budget tests/unit/service/test_callbacks.py::test_drain_due_enforces_https_only_callback_target_policy tests/unit/service/test_callbacks.py::test_drain_due_enforces_callback_target_allowlist_policy tests/unit/service/test_callbacks.py::test_drain_due_rejects_callbacks_with_private_delivery_target_includes_rejected_ip -q`
  - Passed: `5 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/api/test_openapi_artifact.py tests/unit/service/test_callbacks.py -q`
  - Passed: `116 passed`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Passed.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

None.
