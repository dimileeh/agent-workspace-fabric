# Review 4302504110 Callback Production Guardrail Plan

## Problem Statement and Scope

PR review comment `4302504110` reports that callback registration is now protected
by `Depends(require_api_token)`, but production settings validation still rejects
`AWF_CALLBACKS_ENABLED=true` with the old
`production_callbacks_disabled_until_auth` diagnostic. The scope is limited to
production settings guardrails and their regression tests.

## Requirements Checklist

- Allow `AWF_ENV=prod` with a non-default database URL, strong `AWF_API_TOKEN`,
  and `AWF_CALLBACKS_ENABLED=true` to pass production settings validation.
- Preserve rejection of production deployments with missing or weak
  `AWF_API_TOKEN`.
- Preserve sensitive-value redaction in production settings diagnostics.
- Do not weaken callback route authentication.
- Keep changes scoped and covered by unit tests.

## Implementation Steps

1. Update `tests/unit/service/test_config.py` to express the new authenticated
   callback production posture.
2. Run the targeted tests to confirm the old guardrail fails the new expectation.
3. Remove the stale callback-enabled production diagnostic from
   `src/awf/common/config.py`.
4. Re-run the targeted config tests and a focused lint check for touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/config.py tests/unit/service/test_config.py`
  must pass.
