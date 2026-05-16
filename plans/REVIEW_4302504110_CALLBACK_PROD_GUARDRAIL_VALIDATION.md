# Review 4302504110 Callback Production Guardrail Validation

Plan reference:
`plans/REVIEW_4302504110_CALLBACK_PROD_GUARDRAIL_PLAN.md`

## Requirement Status

- Complete: `AWF_ENV=prod` with a non-default database URL, strong
  `AWF_API_TOKEN`, and `AWF_CALLBACKS_ENABLED=true` passes production settings
  validation.
- Complete: production deployments with missing or weak `AWF_API_TOKEN` still
  fail production settings validation.
- Complete: sensitive-value redaction remains covered by existing config tests.
- Complete: callback route authentication was not changed and focused callback
  auth tests still pass.
- Complete: changes are scoped to production config guardrails, config tests,
  and plan/validation documents.

## Evidence

Files changed:

- `src/awf/common/config.py`
- `tests/unit/service/test_config.py`
- `plans/REVIEW_4302504110_CALLBACK_PROD_GUARDRAIL_PLAN.md`
- `plans/REVIEW_4302504110_CALLBACK_PROD_GUARDRAIL_VALIDATION.md`

TDD evidence:

- Before implementation, `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
  failed on the stale `production_callbacks_disabled_until_auth` diagnostic.

Validation commands:

- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
- Passed: `uv run --python 3.12 --extra dev ruff check src/awf/common/config.py tests/unit/service/test_config.py`
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_requires_authorization_token tests/unit/api/test_callbacks.py::test_register_callback_rejects_invalid_authorization_token tests/unit/api/test_callbacks.py::test_list_callbacks_requires_authorization_token tests/unit/api/test_callbacks.py::test_list_callbacks_rejects_invalid_authorization_token -q`

## Gaps

None.
