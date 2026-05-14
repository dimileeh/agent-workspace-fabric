# Review Thread PRRT_kwDOSJAM6s6CLckv Plan

## Problem Statement And Scope

Callback delivery currently validates that a subscription hostname resolves only to public
addresses, then posts to the original hostname through `httpx`. That leaves a DNS
rebinding window between validation and connection. Scope is limited to outbound callback
target validation and delivery in `src/awf/service/callbacks.py` plus focused service tests.

## Requirements Checklist

- Preserve existing callback URL policy checks for scheme, host, userinfo, fragments,
  HTTPS-only mode, allowlists, public host syntax, and public DNS results.
- Return the validated DNS address from delivery-time validation.
- Ensure the default HTTP poster connects to the validated address while preserving the
  original HTTP Host authority and HTTPS SNI/certificate hostname.
- Keep injected test/custom posters compatible with the delivery service contract by
  passing the validated address explicitly.
- Add regression coverage for the DNS rebinding issue.

## Implementation Steps

1. Add a small validated-target result from `_validate_callback_target`.
2. Thread the validated connection address through `CallbackDeliveryService` into the
   callback poster.
3. Update `_httpx_post_json` to rewrite the request URL to the validated IP address when
   provided, set the original Host header, and set the HTTPS SNI hostname extension.
4. Update focused callback service tests and fakes to assert the pinned-address behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  must pass.
