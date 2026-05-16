# Callback Review 4454403868 Plan

## Problem Statement And Scope

PR review comment `issue:4454403868` identifies three callback observability and API
contract gaps:

- `POST /v1/callbacks` can return a structured policy-enforcement `422` response,
  but the route metadata does not declare that structured response shape.
- Callback deliveries that exhaust the total delivery timeout after successful
  target validation are stored as target validation timeouts, which hides that the
  delivery budget was exhausted after validation.
- Callback delivery policy violations from `callbacks_require_https` and
  `callbacks_allowed_hosts` are stored with the same code as malformed URL and
  unsafe resolved-target failures.

Scope is limited to callback API metadata, delivery error-code differentiation,
regression tests, OpenAPI artifact drift, and directly related REST docs.

## Requirements Checklist

- [ ] Declare the structured `422` callback registration response in OpenAPI.
- [ ] Preserve existing retry behavior for callback delivery failures.
- [ ] Store a distinct code for delivery budget exhaustion after successful target
  validation.
- [ ] Store a distinct code for runtime callback target policy violations.
- [ ] Keep structural malformed URL, private IP, DNS, NAT64, and 6to4 rejection
  behavior under the existing invalid-target path unless tests prove otherwise.
- [ ] Add or update focused regression tests before implementation where practical.
- [ ] Regenerate or verify `openapi.json` when the spec changes.

## Implementation Steps

1. Update callback API tests to require the structured `422` response schema for
   `POST /v1/callbacks`.
2. Update callback service tests to expect:
   - `CALLBACK_DELIVERY_BUDGET_EXCEEDED` when validation completes but no POST
     budget remains;
   - `CALLBACK_TARGET_POLICY_VIOLATION` for delivery-time HTTPS/allowlist policy
     mismatches.
3. Update `src/awf/api/routes/callbacks.py` route metadata for the structured
   callback registration `422`.
4. Update `src/awf/service/callbacks.py` to differentiate the two delivery
   error-code cases without changing retry scheduling.
5. Update docs and regenerate `openapi.json`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/api/test_openapi_artifact.py tests/unit/service/test_callbacks.py -q`
  must pass.
- `python scripts/generate_openapi.py --check` must pass after regenerating the
  checked-in artifact.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` must pass for the
  touched Python surface.
